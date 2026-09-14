// Client code for Kanbanano. Plain script, no build step. Loaded before Alpine.

// ---- Card composer --------------------------------------------------------
// One per list, at most one open at a time. The form posts with htmx and the
// server returns just the new card, appended to the list. The composer itself
// is never replaced, so it keeps focus between cards.

document.addEventListener("alpine:init", () => {
  Alpine.data("boardList", () => ({
    composing: false,
    submitted: "", // title of the in-flight request, put back if it fails

    init() {
      const form = this.$refs.form;
      const title = this.$refs.title;

      // htmx has already collected the form values when this fires, so clearing
      // the textarea doesn't affect the request. Anything typed while the
      // request is in flight is the start of the next card.
      form.addEventListener("htmx:configRequest", () => {
        this.submitted = title.value;
        title.value = "";
      });

      // Fires for success, HTTP errors, and network errors alike.
      form.addEventListener("htmx:afterRequest", (event) => {
        if (event.detail.successful) {
          this.$refs.cards.lastElementChild?.scrollIntoView({ block: "nearest" });
        } else {
          title.value = [this.submitted, title.value].filter(Boolean).join(" ");
        }
        this.submitted = "";
      });
    },

    open() {
      for (const list of document.querySelectorAll(".list")) {
        if (list !== this.$root) Alpine.$data(list).close({ refocus: false });
      }
      this.composing = true;
      this.$nextTick(() => this.$refs.title.focus());
    },

    // Hides the composer but keeps any draft text for next time.
    close({ refocus = true } = {}) {
      if (!this.composing) return;
      this.composing = false;
      if (refocus) this.$root.focus(); // so `n` reopens this same list
    },

    onKeydown(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        this.close();
      } else if (event.key === "Enter" && !event.isComposing) {
        event.preventDefault();
        if (this.$refs.title.value.trim()) this.$refs.form.requestSubmit();
      }
    },
  }));
});

// ---- Keyboard shortcuts ---------------------------------------------------

// `n` targets whichever list the pointer or keyboard focus was in most recently.
let currentListId = null;

function trackCurrentList(event) {
  const list = event.target.closest(".list");
  if (list) currentListId = list.dataset.listId;
}
document.addEventListener("mouseover", trackCurrentList);
document.addEventListener("focusin", trackCurrentList);

function currentList() {
  return document.getElementById(`list-${currentListId}`) ?? document.querySelector(".list");
}

function isTyping(el) {
  return el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
}

document.addEventListener("keydown", (event) => {
  if (event.key !== "n" || event.ctrlKey || event.metaKey || event.altKey) return;
  if (isTyping(event.target)) return;

  const list = currentList();
  if (!list) return;
  event.preventDefault(); // otherwise the "n" lands in the textarea we're about to focus
  Alpine.$data(list).open();
});

// ---- Arrow-key navigation -------------------------------------------------
// Up/down moves between cards in a list; from the list itself, Down enters at
// the first card and Up at the last. Left/right moves to the adjacent list,
// landing on the card at the same position (or its last card), or on the list
// itself if it's empty so `n` can add to it. Escape steps out of a card to its
// list. Cards are tabindex="-1": arrows and clicks focus them, but Tab still
// moves between lists and buttons.

const ARROW_MOVES = {
  ArrowUp: [0, -1],
  ArrowDown: [0, 1],
  ArrowLeft: [-1, 0],
  ArrowRight: [1, 0],
};

function cardsIn(list) {
  return [...list.querySelectorAll(".card")];
}

// Focuses the card at `index`, clamped to the last card. Falls back to the
// list itself when it's empty or `index` is -1.
function focusCardAt(list, index) {
  const cards = cardsIn(list);
  (cards[Math.min(index, cards.length - 1)] ?? list).focus();
}

document.addEventListener("keydown", (event) => {
  if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
  if (isTyping(event.target)) return;

  if (event.key === "Escape") {
    event.target.closest(".card")?.closest(".list").focus();
    return;
  }

  const move = ARROW_MOVES[event.key];
  if (!move) return;

  const list = event.target.closest(".list");
  if (!list) {
    // Nothing on the board has focus yet: start at the top of the current list.
    const start = event.target === document.body && currentList();
    if (!start) return;
    event.preventDefault();
    focusCardAt(start, 0);
    return;
  }

  event.preventDefault(); // don't also scroll the list or page
  const [dx, dy] = move;
  const cards = cardsIn(list);
  const index = cards.indexOf(event.target.closest(".card")); // -1: the list itself, or a button in it

  if (dx) {
    const lists = [...document.querySelectorAll(".list")];
    const adjacent = lists[lists.indexOf(list) + dx];
    if (adjacent) focusCardAt(adjacent, index);
  } else if (index === -1) {
    (dy > 0 ? cards[0] : cards.at(-1))?.focus();
  } else {
    cards[index + dy]?.focus();
  }
});

// ---- Errors ---------------------------------------------------------------
// htmx doesn't swap 4xx/5xx responses, so show the server's message instead.

function flash(message) {
  const el = document.getElementById("flash");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(flash.timer);
  flash.timer = setTimeout(() => (el.hidden = true), 4000);
}

document.addEventListener("htmx:responseError", (event) => {
  const { status, responseText } = event.detail.xhr;
  let message = `Something went wrong (${status})`;
  try {
    const { detail } = JSON.parse(responseText); // FastAPI's HTTPException body
    if (typeof detail === "string") message = detail;
  } catch {}
  flash(message);
});

document.addEventListener("htmx:sendError", () => {
  flash("Couldn't reach the server. Check your connection.");
});
