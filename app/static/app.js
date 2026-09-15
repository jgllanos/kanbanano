// Client code for Kanbanano. Plain script, no build step. Loaded before Alpine.

// ---- Composers ------------------------------------------------------------
// Both the card composer and the "add a list" composer stay in the page between
// submissions, so they keep focus and anything typed mid-request survives. This
// wires up the part they share: the field empties as the request goes out, so
// the user can start the next one immediately, and fills back in if it fails.
// `onAdded` runs after a successful add.

function composerForm(form, field, onAdded) {
  let submitted = ""; // text of the in-flight request, put back if it fails

  // htmx has already collected the form values when this fires, so clearing the
  // field doesn't affect the request. Anything typed while it's in flight is the
  // start of the next card or list.
  form.addEventListener("htmx:configRequest", () => {
    submitted = field.value;
    field.value = "";
  });

  // Fires for success, HTTP errors, and network errors alike.
  form.addEventListener("htmx:afterRequest", (event) => {
    if (event.detail.successful) onAdded();
    else field.value = [submitted, field.value].filter(Boolean).join(" ");
    submitted = "";
  });
}

// ---- List: card composer, and renaming ------------------------------------
// One card composer per list, at most one open at a time. The server returns
// just the new card, appended to the list. Renaming swaps the heading for a
// field in place; see _list.html.

document.addEventListener("alpine:init", () => {
  Alpine.data("boardList", () => ({
    composing: false,
    renaming: false,

    init() {
      composerForm(this.$refs.form, this.$refs.title, () => {
        const card = this.$refs.cards.lastElementChild;
        card?.classList.add("just-added"); // exempt from the "Assigned to me" filter
        card?.scrollIntoView({ block: "nearest" });
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

  // ---- "Add a list" composer ----------------------------------------------
  // The last column of the board. New lists are swapped in just before it, so
  // it stays at the right-hand end and the next title can be typed straight in.

  Alpine.data("addList", () => ({
    composing: false,

    init() {
      composerForm(this.$refs.form, this.$refs.title, () =>
        this.$root.scrollIntoView({ block: "nearest", inline: "nearest" }),
      );
    },

    open() {
      this.composing = true;
      this.$nextTick(() => this.$refs.title.focus());
    },

    // Hides the form but keeps any draft text for next time, as the card composer does.
    close() {
      this.composing = false;
    },
  }));

  // ---- Card modal ---------------------------------------------------------
  // GET /cards/{id} swaps a <dialog> into #modal-root, and it opens itself.
  // Closing leaves it in place (closed) until the next card replaces it, so
  // a save that closing set off can still finish and report back here.
  // Errors from anything inside the modal are shown in the modal.

  Alpine.data("cardModal", (cardId, listId) => ({
    error: "",
    canReload: false, // offer "Reload card" after a conflict

    init() {
      this.$root.showModal();
      this.$root.addEventListener("htmx:afterRequest", (event) => this.afterRequest(event.detail));
      this.$root.addEventListener("close", () => {
        // The card if it still exists, otherwise its list.
        const card = document.getElementById(`card-${cardId}`);
        (card ?? document.getElementById(`list-${listId}`))?.focus();
      });
    },

    afterRequest({ successful, xhr, elt }) {
      if (successful) {
        this.error = "";
        if (elt === this.$refs.deleteButton) this.$root.close();
        return;
      }
      // A save set off by closing the modal failed: reopen so the text isn't lost.
      if (!this.$root.open) this.$root.showModal();
      this.error = xhr.status ? errorMessage(xhr.status, xhr.responseText) : OFFLINE_MESSAGE;
      this.canReload = xhr.status === 409;
    },
  }));

  // ---- "I am" picker and "Assigned to me" filter --------------------------
  // Who you are is a per-device preference, not a credential: a person's id
  // per board, in localStorage. Keyed by id so renaming a person doesn't
  // break it. The server re-renders this widget when people change; init()
  // then restores the saved state.

  Alpine.data("identity", (boardId) => ({
    me: "",
    mine: false,

    init() {
      const saved = storage.get(`me:${boardId}`) ?? "";
      const stillExists = [...this.$refs.select.options].some((o) => o.value === saved);
      this.me = stillExists ? saved : "";
      this.mine = this.me !== "" && storage.get(`mine:${boardId}`) === "1";

      this.$watch("me", (me) => {
        storage.set(`me:${boardId}`, me);
        if (!me) this.mine = false;
        this.apply();
      });
      this.$watch("mine", (mine) => {
        storage.set(`mine:${boardId}`, mine ? "1" : "");
        this.apply();
      });
      this.apply();
    },

    apply() {
      setMineFilter(this.mine ? this.me : null);
    },
  }));
});

// localStorage can be unavailable (private windows, blocked storage); then
// preferences just don't persist.
const storage = {
  get(key) {
    try {
      return localStorage.getItem(`kanbanano:${key}`);
    } catch {
      return null;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(`kanbanano:${key}`, value);
    } catch {}
  },
};

// The filter is a generated style rule rather than a pass over the cards, so
// it also applies to cards added or re-rendered later. Cards added with the
// composer while it's on are exempt, so they don't vanish as you create them.
const mineFilterStyle = document.createElement("style");
document.head.append(mineFilterStyle);

function setMineFilter(personId) {
  const id = Number.parseInt(personId, 10); // interpolated into CSS: keep it a number
  mineFilterStyle.textContent = Number.isInteger(id)
    ? `.card:not(.just-added):not(:has(.person-chip[data-person-id="${id}"])) { display: none; }`
    : "";
}

function modalOpen() {
  return document.querySelector("#modal-root dialog[open]") !== null;
}

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
  if (isTyping(event.target) || modalOpen()) return;

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

// Visible cards only: the "Assigned to me" filter hides the rest.
function cardsIn(list) {
  return [...list.querySelectorAll(".card")].filter((card) => card.checkVisibility());
}

// Focuses the card at `index`, clamped to the last card. Falls back to the
// list itself when it's empty or `index` is -1.
function focusCardAt(list, index) {
  const cards = cardsIn(list);
  (cards[Math.min(index, cards.length - 1)] ?? list).focus();
}

document.addEventListener("keydown", (event) => {
  if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
  if (isTyping(event.target) || modalOpen()) return;

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

// The card modal shows its own errors (see cardModal).
const inModal = (event) => event.detail.elt.closest("#modal-root") !== null;

document.addEventListener("htmx:responseError", (event) => {
  if (inModal(event)) return;
  const { status, responseText } = event.detail.xhr;
  flash(errorMessage(status, responseText));
});

document.addEventListener("htmx:sendError", (event) => {
  if (!inModal(event)) flash(OFFLINE_MESSAGE);
});

// ---- Drag and drop --------------------------------------------------------
// SortableJS moves the element in the DOM itself; we then post the full new
// order of every list the drag touched. The server replies 204, so there's
// nothing to swap. If the save fails, the element goes back where it was.
//
// This uses fetch rather than htmx: htmx queues requests per element and can
// drop a queued one, which here would silently lose a move.
//
// body.dragging is set for the duration of a drag. It enlarges empty lists as
// drop targets, and holds off board refreshes mid-drag (see Polling).

function idsIn(container, selector, attribute) {
  return [...container.querySelectorAll(selector)].map((el) => el.getAttribute(attribute));
}

// Undoes a Sortable move by putting the element back at its old index.
function undoMove({ item, from, oldIndex }) {
  item.remove();
  from.insertBefore(item, from.children[oldIndex] ?? null);
}

async function saveOrder(url, body, drop) {
  pendingWrites++; // htmx requests are counted by listeners; fetch has to do it itself
  try {
    const response = await fetch(url, { method: "POST", body });
    if (response.ok) {
      adoptVersion(response.headers.get("X-Board-Version"));
    } else {
      flash(errorMessage(response.status, await response.text()));
      undoMove(drop);
    }
  } catch {
    flash(OFFLINE_MESSAGE);
    undoMove(drop);
  } finally {
    pendingWrites--;
    refreshIfStale();
  }
}

function endDrag() {
  document.body.classList.remove("dragging");
  refreshSoon(); // runs after saveOrder, if the drop starts one, has counted itself
}

const dragOptions = {
  animation: 150,
  ghostClass: "drag-ghost",
  delay: 150, // on touch screens, a short hold starts a drag so swiping still scrolls
  delayOnTouchOnly: true,
  onStart: () => document.body.classList.add("dragging"),
};

function onCardDrop(drop) {
  endDrag();
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
  endDrag();
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
      // Keeps the "add a list" column at the end: it's the only child that
      // isn't a list, and a list dropped past it would look out of place until
      // the next refresh put it back.
      onMove: ({ related }) => related.classList.contains("list"),
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

// ---- Polling --------------------------------------------------------------
// #board-poller asks every 3s whether the board has moved past the version in
// #lists-container's data-version. If it has, the server sends the whole lists
// container and it's swapped in. This is only for seeing other people's
// changes: our own are shown straight from their responses.
//
// A refresh must not wreck what the user is in the middle of. It's held back,
// and the board marked stale, only when it would replace something in use: a
// form field it re-renders has focus (a composer, a title field, the "I am"
// picker), a drag
// is under way, or one of our own saves is in flight. It then catches up as
// soon as none of those hold. An open modal doesn't hold it back: the modal is
// outside the refreshed area, so the board behind it stays live. A refresh
// keeps scroll positions, composer drafts, and focus (htmx refocuses by id).

let stale = false; // a poll found changes that couldn't be shown yet
let pendingWrites = 0; // our own saves in flight
let restoreBoardState = null; // set just before a refresh swap, run right after

function boardVersion() {
  return document.getElementById("lists-container")?.dataset.version;
}

// The poller's `every 3s` filter. No need to ask while we already know the
// board is stale, or while the tab is in the background.
function pollAllowed() {
  return !stale && !document.hidden;
}

// Everything a refresh replaces: the lists, and the header out-of-band.
const REFRESHED_AREA = "#lists-container, #board-header";

function refreshBlocked() {
  const active = document.activeElement;
  return (
    (isTyping(active) && active.closest(REFRESHED_AREA) !== null) ||
    document.body.classList.contains("dragging") ||
    pendingWrites > 0
  );
}

function refreshIfStale() {
  const poller = document.getElementById("board-poller");
  if (!poller || !stale || refreshBlocked()) return;
  stale = false;
  htmx.trigger(poller, "refresh");
}

// Deferred a tick so focus has settled on its new element, and any save that
// the blur set off has started (and so counts as pending). Completed saves
// call refreshIfStale directly.
function refreshSoon() {
  setTimeout(refreshIfStale);
}

document.addEventListener("focusout", refreshSoon);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  stale = true; // polling paused while hidden, so we may have missed changes
  refreshIfStale();
});

// Our own saves bump the version too, and report the new one in a header.
// Taking it on means the next poll won't refetch a change we already show.
// Only safe when it's exactly one past ours: anything more means someone else
// changed something too, which the poll still needs to fetch.
function adoptVersion(header) {
  const container = document.getElementById("lists-container");
  if (container && header && Number(header) === Number(container.dataset.version) + 1) {
    container.dataset.version = header;
  }
}

const isWrite = (event) => event.detail.requestConfig.verb !== "get";

document.addEventListener("htmx:beforeRequest", (event) => {
  if (isWrite(event)) pendingWrites++;
});

document.addEventListener("htmx:afterRequest", (event) => {
  if (!isWrite(event)) return;
  pendingWrites--;
  if (event.detail.successful) {
    adoptVersion(event.detail.xhr.getResponseHeader("X-Board-Version"));
  }
  refreshIfStale();
});

// Swap events fire on the swap target, and htmx sets detail.elt to whatever the
// event fired on, so the element that made the request is in requestConfig.
const fromPoller = (event) => event.detail.requestConfig?.elt.id === "board-poller";

document.addEventListener("htmx:beforeSwap", (event) => {
  if (!fromPoller(event) || !event.detail.shouldSwap) return;
  if (refreshBlocked()) {
    event.detail.shouldSwap = false;
    stale = true;
  } else {
    restoreBoardState = snapshotBoard();
  }
});

document.addEventListener("htmx:afterSwap", (event) => {
  if (!fromPoller(event) || !restoreBoardState) return;
  restoreBoardState();
  restoreBoardState = null;
});

// Captures what a refresh would otherwise lose, and returns a function that
// puts it back: scroll positions, composer drafts, and which cards were just
// added (and so exempt from the "Assigned to me" filter).
function snapshotBoard() {
  const container = document.getElementById("lists-container");
  const scrollLeft = container.scrollLeft;
  const lists = [...container.querySelectorAll(".list")].map((list) => ({
    id: list.id,
    scrollTop: list.querySelector(".cards").scrollTop,
    draft: list.querySelector(".composer textarea").value,
  }));
  const justAdded = [...container.querySelectorAll(".card.just-added")].map((card) => card.id);
  const newListDraft = container.querySelector("#add-list input[name=title]").value;

  return () => {
    document.getElementById("lists-container").scrollLeft = scrollLeft;
    for (const { id, scrollTop, draft } of lists) {
      const list = document.getElementById(id);
      if (!list) continue; // someone else deleted it
      list.querySelector(".cards").scrollTop = scrollTop;
      list.querySelector(".composer textarea").value = draft;
    }
    document.querySelector("#add-list input[name=title]").value = newListDraft;
    for (const id of justAdded) document.getElementById(id)?.classList.add("just-added");
  };
}
