// Client code for Kanbanano. Plain script, no build step. Loaded before Alpine.

// ---- Composers ------------------------------------------------------------
// The card composer and the "add a list" composer stay on the page between
// submissions, so they keep focus. This handles what they share: the field is
// cleared when the request is sent, so the next entry can be typed right away,
// and restored if the request fails. `onAdded` runs after a successful add.

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
// One card composer per list, and at most one open at a time. The server returns
// the new card, which is appended to the list. Renaming replaces the heading
// with a field (see _list_header.html).

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
  // The last column of the board. New lists are inserted before it, so it stays
  // at the end and the next title can be typed right away.

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

  // ---- Checklist add-item form --------------------------------------------
  // It's outside the part of the checklist that writes replace, so it keeps
  // focus between items.

  Alpine.data("checklistAdd", () => ({
    init() {
      composerForm(this.$refs.form, this.$refs.text, () =>
        this.$refs.form.scrollIntoView({ block: "nearest" }),
      );
    },
  }));

  // ---- Card modal ---------------------------------------------------------
  // GET /cards/{id} swaps a <dialog> into #modal-root, and it opens itself.
  // Closing leaves it in place (closed) until the next card replaces it, so a
  // save triggered by closing can still finish and report errors here.
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
      // A save triggered by closing the modal failed. Reopen it so the text isn't lost.
      if (!this.$root.open) this.$root.showModal();
      this.error = xhr.status ? errorMessage(xhr.status, xhr.responseText) : OFFLINE_MESSAGE;
      this.canReload = xhr.status === 409;
    },
  }));

  // ---- "I am" picker and "Assigned to me" filter --------------------------
  // Who you are is a per-device preference: a person's id for each board, saved
  // in localStorage. It uses the id so renaming a person doesn't break it. When
  // the server re-renders this widget, init() restores the saved state.

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

// The filter is a generated CSS rule, so it also applies to cards added or
// re-rendered later. Cards added with the composer while it's on are exempt, so
// they don't disappear as you create them.
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
// SortableJS moves the element in the DOM, then we post the new order of every
// list the drag touched. The server replies 204. If the save fails, the element
// is moved back.
//
// This uses fetch instead of htmx, because htmx queues requests per element and
// can drop a queued one, which would lose a move.
//
// body.dragging is set during a drag. It makes empty lists bigger drop targets
// and holds back board refreshes (see Polling).

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
    if (response.status === 401) {
      // The session has expired. fetch ignores the HX-Redirect header that htmx
      // would follow, so redirect here.
      location.href = "/login";
    } else if (response.ok) {
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
  refreshSoon(); // deferred, so a save started by this drop is already counted
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
      // Keep the "add a list" column last by refusing moves next to anything
      // that isn't a list.
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
// Every 3s, #board-poller asks whether the board is newer than the version in
// #lists-container's data-version. If it is, the server sends the lists
// container, which is swapped in. Polling is only for other people's changes;
// our own come back in the responses to our requests.
//
// A refresh is held back, and the board marked stale, when it would replace
// something in use: a focused field inside the refreshed area (a composer, a
// title field, the "I am" picker), a drag in progress, or one of our saves in
// flight. It runs as soon as none of those apply. An open modal doesn't hold it
// back, because the modal is outside the refreshed area. A refresh keeps scroll
// positions, composer drafts and focus (htmx restores focus by id).

let stale = false; // a poll found changes that couldn't be shown yet
let pendingWrites = 0; // our own saves in flight
let restoreBoardState = null; // set just before a refresh swap, run right after

function boardVersion() {
  return document.getElementById("lists-container")?.dataset.version;
}

// The poller's `every 3s` filter: skip polling while the board is already known
// to be stale, or while the tab is hidden.
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

// Waits a tick so focus has moved to its new element and any save triggered by
// the blur has started (and counts as pending). Finished saves call
// refreshIfStale directly.
function refreshSoon() {
  setTimeout(refreshIfStale);
}

document.addEventListener("focusout", refreshSoon);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  stale = true; // polling paused while hidden, so we may have missed changes
  refreshIfStale();
});

// Our own saves bump the version too, and return the new one in a header.
// Adopting it stops the next poll refetching a change we already show. It's only
// adopted if it's exactly one more than ours: a bigger jump means someone else
// changed something that the poll still needs to fetch.
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
