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

const OFFLINE_MESSAGE = "Couldn't reach the server. Check your connection.";

function errorMessage(status, body) {
  try {
    const { detail } = JSON.parse(body); // FastAPI's HTTPException body
    if (typeof detail === "string") return detail;
  } catch {}
  return `Something went wrong (${status})`;
}

document.addEventListener("htmx:responseError", (event) => {
  const { status, responseText } = event.detail.xhr;
  flash(errorMessage(status, responseText));
});

document.addEventListener("htmx:sendError", () => flash(OFFLINE_MESSAGE));

// ---- Drag and drop --------------------------------------------------------
// SortableJS moves the element in the DOM itself; we then post the full new
// order of every list the drag touched. The server replies 204, so there's
// nothing to swap. If the save fails, the element goes back where it was.
//
// This uses fetch rather than htmx: htmx queues requests per element and can
// drop a queued one, which here would silently lose a move.
//
// body.dragging is set for the duration of a drag. It enlarges empty lists as
// drop targets, and board polling will use it to hold off refreshing mid-drag.

function idsIn(container, selector, attribute) {
  return [...container.querySelectorAll(selector)].map((el) => el.getAttribute(attribute));
}

// Undoes a Sortable move by putting the element back at its old index.
function undoMove({ item, from, oldIndex }) {
  item.remove();
  from.insertBefore(item, from.children[oldIndex] ?? null);
}

async function saveOrder(url, body, drop) {
  try {
    const response = await fetch(url, { method: "POST", body });
    if (response.ok) return;
    flash(errorMessage(response.status, await response.text()));
  } catch {
    flash(OFFLINE_MESSAGE);
  }
  undoMove(drop);
}

const dragOptions = {
  animation: 150,
  ghostClass: "drag-ghost",
  delay: 150, // on touch screens, a short hold starts a drag so swiping still scrolls
  delayOnTouchOnly: true,
  onStart: () => document.body.classList.add("dragging"),
};

function onCardDrop(drop) {
  document.body.classList.remove("dragging");
  const { from, to } = drop;
  if (from === to && drop.oldIndex === drop.newIndex) return;

  const body = new URLSearchParams({ list_id: to.dataset.listId });
  for (const id of idsIn(to, ".card", "data-card-id")) body.append("card_ids", id);
  if (from !== to) {
    body.append("from_list_id", from.dataset.listId);
    for (const id of idsIn(from, ".card", "data-card-id")) body.append("from_card_ids", id);
  }
  saveOrder("/cards/reorder", body, drop);
}

function onListDrop(drop) {
  document.body.classList.remove("dragging");
  if (drop.oldIndex === drop.newIndex) return;

  const container = drop.to;
  const body = new URLSearchParams({ board_id: container.dataset.boardId });
  for (const id of idsIn(container, ":scope > .list", "data-list-id")) body.append("list_ids", id);
  saveOrder("/lists/reorder", body, drop);
}

// Attaches Sortable to any container that doesn't have it yet. Safe to call
// repeatedly: htmx swaps replace elements, and their Sortable instances with them.
function initSortables() {
  const container = document.getElementById("lists-container");
  if (!container) return;

  if (!Sortable.get(container)) {
    Sortable.create(container, {
      ...dragOptions,
      draggable: ".list",
      handle: ".list-title",
      onEnd: onListDrop,
    });
  }
  for (const cards of container.querySelectorAll(".cards")) {
    if (Sortable.get(cards)) continue;
    Sortable.create(cards, {
      ...dragOptions,
      group: "cards", // lets cards move between lists
      draggable: ".card",
      onEnd: onCardDrop,
    });
  }
}

document.addEventListener("DOMContentLoaded", initSortables);
document.addEventListener("htmx:afterSwap", initSortables);
