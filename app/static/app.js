// Client code for Kanbanano. Plain script, no build step. Loaded before Alpine.

// When htmx swaps in an element whose id is already on the page, it copies the
// old element's class and style onto it, then restores the new element's own
// attributes after settling. That restore removes the inline style Alpine's
// x-show uses to hide things, so a closed menu that a poll re-rendered would pop
// open. Settling only matters for CSS transitions, which this app doesn't use.
htmx.config.attributesToSettle = [];

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
    leftOff: [], // ids of filter labels and people left off new cards, until the composer closes

    init() {
      composerForm(this.$refs.form, this.$refs.title, () => {
        const card = this.$refs.cards.lastElementChild;
        card?.classList.add("just-added"); // exempt from the board filter
        card?.scrollIntoView({ block: "nearest" });
      });
      // Changing the filter brings back anything left off.
      this.$watch("[$store.filter.selected, $store.filter.match]", () => (this.leftOff = []));
    },

    // The labels and people a new card gets from the filter, minus any left off.
    get defaults() {
      return this.$store.filter.newCardOptions.filter((option) => !this.leftOff.includes(option.id));
    },

    leaveOff(id) {
      this.leftOff = [...this.leftOff, id];
      this.$refs.title.focus();
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
      this.leftOff = [];
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

  // ---- Menus --------------------------------------------------------------
  // Board settings and list settings. An open menu holds back board refreshes,
  // which would otherwise close it (see refreshBlocked), so closing one lets a
  // waiting refresh through.

  Alpine.data("menu", () => ({
    open: false,

    init() {
      this.$watch("open", (open) => {
        if (!open) refreshSoon();
      });
    },

    close() {
      this.open = false;
      this.$refs.menuButton.focus();
    },
  }));

  // ---- Card modal ---------------------------------------------------------
  // GET /cards/{id} swaps a <dialog> into #modal-root, and it opens itself.
  // Closing leaves it in place (closed) until the next card replaces it, so a
  // save triggered by closing can still finish and report errors here.
  // Errors from anything inside the modal are shown in the modal, except from
  // the parts that report their own (see SELF_REPORTING).

  // Sections of the modal with their own error area. A failure inside one
  // belongs there, next to the thing that failed, not in the bar at the top.
  const SELF_REPORTING = "#description-form";

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

    // Every way out of the modal comes through here, so that an unfinished edit
    // inside it gets to object first. Anything holding unsaved work marks
    // itself data-unsaved; the description editor is the only one so far.
    close() {
      const unsaved = this.$root.querySelector("[data-unsaved]");
      if (unsaved && !confirm("Close the card and lose your unsaved description?")) return;
      this.$root.close();
    },

    afterRequest({ successful, xhr, elt }) {
      if (successful) {
        this.error = "";
        if (elt === this.$refs.archiveButton) this.$root.close();
        return;
      }
      if (elt.closest(SELF_REPORTING)) return;
      // A save triggered by closing the modal failed. Reopen it so the text isn't lost.
      if (!this.$root.open) this.$root.showModal();
      this.error = xhr.status ? errorMessage(xhr.status, xhr.responseText) : OFFLINE_MESSAGE;
      this.canReload = xhr.status === 409;
    },
  }));

  // ---- Card description ---------------------------------------------------
  // Markdown, rendered by the server. The modal shows the rendered HTML, the
  // Edit button swaps in a textarea, and nothing is saved until Save is
  // pressed. The response brings the re-rendered HTML back with it (see
  // _card_saved.html), so closing the editor lands on the saved version.
  //
  // `original` is the description as the server last confirmed it: it's what
  // Cancel restores and what `dirty` is measured against. Nothing here reads
  // the rendered HTML, only the textarea.
  //
  // This block reports its own errors, in its own footer, rather than in the
  // modal's error bar (see cardModal).

  Alpine.data("descriptionEditor", () => ({
    editing: false,
    dirty: false,
    original: "", // the description as last saved; Cancel goes back to it
    hasText: false, // drives the Edit/Add label
    error: "",
    conflict: null, // their version and token, once a save is rejected

    init() {
      this.original = this.$refs.field.value;
      this.hasText = this.original.trim() !== "";
      // The form is inside this element, so its htmx events bubble through here.
      this.$el.addEventListener("htmx:afterRequest", ({ detail }) => this.afterSave(detail));
      this.$el.addEventListener("htmx:sendError", () => (this.error = OFFLINE_MESSAGE));
    },

    edit() {
      this.editing = true;
      this.$nextTick(() => {
        const field = this.$refs.field;
        field.focus();
        field.setSelectionRange(field.value.length, field.value.length); // type at the end
      });
    },

    save() {
      // Nothing to send: an empty save would still move updated_at, which would
      // turn everyone else's open modal into a conflict for no reason.
      if (!this.dirty) return this.cancel();
      this.$refs.form.requestSubmit(); // htmx picks the submit up
    },

    cancel() {
      this.$refs.field.value = this.original;
      this.dirty = false;
      this.editing = false;
      this.conflict = null;
      this.error = "";
    },

    afterSave({ successful, xhr }) {
      if (successful) {
        this.original = this.$refs.field.value;
        this.hasText = this.original.trim() !== "";
        this.dirty = false;
        this.editing = false; // onto the HTML that came back with the response
        this.conflict = null;
        this.error = "";
        return;
      }
      if (!xhr.status) return (this.error = OFFLINE_MESSAGE);
      this.error = errorMessage(xhr.status, xhr.responseText);
      // A 409 carries the version this save lost to, so the three ways out
      // below can each work from it. Anything else is just a message.
      this.conflict = xhr.status === 409 ? conflictFrom(xhr.responseText) : null;
      if (this.conflict) updatedAtField().value = this.conflict.updated_at;
    },

    // Their version loses: the token has already been moved on to theirs, so
    // sending the same text again overwrites it.
    keepMine() {
      this.conflict = null;
      this.error = "";
      this.$refs.form.requestSubmit();
    },

    // Neither version is thrown away. The editor stays open on the two of them
    // for the actual merging, which only a person can do.
    keepBoth() {
      const field = this.$refs.field;
      field.value = [field.value, this.conflict.description].filter(Boolean).join("\n\n---\n\n");
      this.dirty = true;
      this.conflict = null;
      this.error = "";
      field.focus();
    },
  }));

  // ---- Archive dialog -----------------------------------------------------
  // GET /boards/{id}/archive swaps a <dialog> into #modal-root and it opens
  // itself, like the card modal. Restoring or deleting swaps the body, so the
  // dialog's own errors are shown here, and a flash message would be behind it.

  Alpine.data("archiveModal", () => ({
    error: "",

    init() {
      this.$root.showModal();
      this.$root.addEventListener("htmx:afterRequest", ({ detail }) => {
        const { successful, xhr } = detail;
        if (successful) this.error = "";
        else this.error = xhr.status ? errorMessage(xhr.status, xhr.responseText) : OFFLINE_MESSAGE;
      });
      this.$root.addEventListener("close", () =>
        document.querySelector(".board-menu-open")?.focus(),
      );
    },
  }));

  // ---- Board filter -------------------------------------------------------
  // A per-device preference for each board, saved in localStorage. It uses ids,
  // so renaming a label or person doesn't break it. It shows cards with all of
  // the selected labels and people, or any of them. With "all", new cards get
  // them too (see boardList).
  //
  // The store holds the state. The widget in the header (_filter.html) hands it
  // the board's labels and people each time the server renders it, which also
  // drops anything deleted since.

  // How many selected labels and people the filter button shows by name.
  const SUMMARY_LIMIT = 3;

  Alpine.store("filter", {
    boardId: null,
    options: [], // the board's labels, then its people: {id, kind, name, color, initials}
    selected: [], // label and person ids to filter by. Ids are unique across both.
    match: "all", // "all" or "any" of the selected

    init() {
      Alpine.effect(() => {
        if (this.boardId === null) return; // not on a board
        const { selected, match } = this;
        storage.set(`filter:${this.boardId}`, JSON.stringify({ selected, match }));
        setBoardFilter(selected, match);
      });
    },

    load(boardId, options) {
      let saved = {};
      try {
        saved = JSON.parse(storage.get(`filter:${boardId}`)) ?? {};
      } catch {}
      const known = new Set(options.map((option) => option.id));
      const selected = Array.isArray(saved.selected) ? saved.selected.filter((id) => known.has(id)) : [];
      const match = saved.match === "any" ? "any" : "all";
      Object.assign(this, { boardId, options, selected, match });
    },

    get count() {
      return this.selected.length;
    },

    get selectedOptions() {
      return this.options.filter((option) => this.selected.includes(option.id));
    },

    // The labels and people a new card gets, so it matches the filter. With
    // "any" that would be too many: one of them would do, and which is unclear.
    get newCardOptions() {
      return this.match === "all" ? this.selectedOptions : [];
    },

    // For the filter button: the first few by name, then a count of the rest.
    get shownOptions() {
      return this.selectedOptions.slice(0, SUMMARY_LIMIT);
    },

    get hiddenCount() {
      return Math.max(0, this.count - SUMMARY_LIMIT);
    },

    has(id) {
      return this.selected.includes(id);
    },

    toggle(id) {
      this.selected = this.has(id) ? this.selected.filter((other) => other !== id) : [...this.selected, id];
    },

    clear() {
      this.selected = [];
    },
  });

  Alpine.data("boardFilter", (boardId, options) => ({
    init() {
      this.$store.filter.load(boardId, options);
    },
  }));

  // ---- Lists panel --------------------------------------------------------
  // A panel down the left listing every list on the board; clicking one shows
  // or hides it. Per device and per board in localStorage, like the filter, and
  // applied the same way, as a generated CSS rule.
  //
  // The store holds the state. The panel (_list_rail.html) reads its items from
  // the board, so a list added, renamed, deleted or dragged needs no server
  // support.

  Alpine.store("rail", {
    boardId: null,
    hidden: [], // ids of lists kept off the board
    collapsed: false, // the panel itself is a thin strip

    init() {
      Alpine.effect(() => {
        if (this.boardId === null) return; // not on a board
        const { hidden, collapsed } = this;
        storage.set(`rail:${this.boardId}`, JSON.stringify({ hidden, collapsed }));
        setHiddenLists(hidden);
      });
    },

    load(boardId) {
      let saved = {};
      try {
        saved = JSON.parse(storage.get(`rail:${boardId}`)) ?? {};
      } catch {}
      const hidden = Array.isArray(saved.hidden) ? saved.hidden.filter(Number.isInteger) : [];
      Object.assign(this, { boardId, hidden, collapsed: saved.collapsed === true });
    },

    get count() {
      return this.hidden.length;
    },

    isHidden(id) {
      return this.hidden.includes(id);
    },

    toggle(id) {
      this.hidden = this.isHidden(id) ? this.hidden.filter((other) => other !== id) : [...this.hidden, id];
    },

    // Called from the list's own menu (_list_header.html). The list disappears
    // while the menu inside it has the focus, so move the focus to the panel.
    hide(id) {
      if (!this.isHidden(id)) this.hidden = [...this.hidden, id];
      Alpine.nextTick(() => focusRailItem(id));
    },

    // Called with the board's list ids, to forget any deleted since.
    prune(ids) {
      const known = new Set(ids);
      if (this.hidden.some((id) => !known.has(id))) {
        this.hidden = this.hidden.filter((id) => known.has(id));
      }
    },
  });

  Alpine.data("boardRail", (boardId) => ({
    lists: [], // {id, title} for every list on the board, hidden or not

    init() {
      this.$store.rail.load(boardId);
      this.read();
      // Nothing tells the panel when a list is added, renamed, deleted or
      // dragged, and a poll replaces #lists-container itself. Watching the
      // parent element for any change covers all of those.
      new MutationObserver(() => this.read()).observe(this.$root.parentElement, {
        childList: true,
        subtree: true,
      });
    },

    // Rebuilds the items from the board. Rendering the panel is itself a change
    // the observer sees, so this returns early when the result is the same and
    // the observer stops after one extra pass.
    read() {
      const lists = [...document.querySelectorAll("#lists-container .list")].map((list) => ({
        id: Number(list.dataset.listId),
        title: list.querySelector(".list-title").textContent.trim(),
      }));
      if (JSON.stringify(lists) === JSON.stringify(this.lists)) return;
      this.lists = lists;
      this.$store.rail.prune(lists.map((list) => list.id));
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

// The board filter is a generated CSS rule, so it also applies to cards added or
// re-rendered later. With "all" it hides a card missing any selected label or
// person; with "any", a card missing every one of them. Cards added with the
// composer are exempt, so a card doesn't disappear as you create it, even if it
// didn't get the filter's labels.
const boardFilterStyle = document.createElement("style");
document.head.append(boardFilterStyle);

function setBoardFilter(ids, match) {
  const tags = ids
    .filter(Number.isInteger) // interpolated into CSS: keep them numbers
    .map((id) => `[data-label-id="${id}"], [data-person-id="${id}"]`);
  const missing = match === "any" ? [tags.join(", ")] : tags;
  const rules = tags.length ? missing.map((tag) => `.card:not(.just-added):not(:has(${tag}))`) : [];
  boardFilterStyle.textContent = rules.length ? `${rules.join(",\n")} { display: none; }` : "";
}

// Lists hidden from the panel, as a generated CSS rule, so hiding survives a
// board refresh re-rendering the lists. A hidden list is still in the page, so
// a drag posts the order of every list, including the hidden ones.
const hiddenListStyle = document.createElement("style");
document.head.append(hiddenListStyle);

function setHiddenLists(ids) {
  const rules = ids.filter(Number.isInteger).map((id) => `.list[data-list-id="${id}"]`);
  hiddenListStyle.textContent = rules.length ? `${rules.join(",\n")} { display: none; }` : "";
}

// Focuses a list in the panel, or the panel's own button when it's a strip.
function focusRailItem(listId) {
  const rail = document.getElementById("list-rail");
  const item = rail?.querySelector(`.rail-list[data-list-id="${listId}"]`);
  (item?.checkVisibility() ? item : rail?.querySelector(".rail-toggle"))?.focus();
}

function modalOpen() {
  return document.querySelector("#modal-root dialog[open]") !== null;
}

// ---- Card links -----------------------------------------------------------
// /boards/1#card-5 opens card 5 once the board has loaded. The focus timer links
// to its card this way from other pages. htmx sets up the card's click handler
// on DOMContentLoaded too, and its listener was added first, so it's ready here.

document.addEventListener("DOMContentLoaded", () => {
  const match = location.hash.match(/^#card-(\d+)$/);
  if (!match) return;
  history.replaceState(null, "", location.pathname + location.search);
  document.getElementById(`card-${match[1]}`)?.click();
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

// Lists hidden from the lists panel are skipped, so the keyboard only reaches
// lists that are on the board.
function visibleLists() {
  return [...document.querySelectorAll(".list")].filter((list) => list.checkVisibility());
}

function currentList() {
  const lists = visibleLists();
  const current = document.getElementById(`list-${currentListId}`);
  return lists.includes(current) ? current : lists[0];
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

// Visible cards only: the board filter hides the rest.
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
    const lists = visibleLists();
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

// The card's shared conflict token. Both of the modal's forms send it, and a
// save's response replaces it (see _card_modal.html).
const updatedAtField = () => document.getElementById("card-updated-at");

// A rejected description save: the stored version and the token it's now at
// (see update_card). Null if the body isn't one, so a 409 from anywhere else
// is reported as a plain message.
function conflictFrom(body) {
  try {
    const { description, updated_at } = JSON.parse(body);
    if (typeof description === "string" && updated_at) return { description, updated_at };
  } catch {}
  return null;
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
// title field), an open menu, a drag in progress, or one of our saves in flight.
// It runs as soon as none of those apply. An open modal doesn't hold it back,
// because the modal is outside the refreshed area. A refresh keeps scroll
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
    document.querySelector(".menu.is-open")?.closest(REFRESHED_AREA) != null ||
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

// A restore from the archive dialog sends the board with it, out-of-band, which
// replaces the lists without going through the poller. Keep what a refresh
// keeps, above all the sideways scroll position.
const replacesBoard = (event) => event.detail.target?.id === "lists-container";

document.addEventListener("htmx:oobBeforeSwap", (event) => {
  if (replacesBoard(event)) restoreBoardState = snapshotBoard();
});

document.addEventListener("htmx:oobAfterSwap", (event) => {
  if (!replacesBoard(event) || !restoreBoardState) return;
  restoreBoardState();
  restoreBoardState = null;
});

// Captures what a refresh would otherwise lose, and returns a function that
// puts it back: scroll positions, composer drafts, and which cards were just
// added (and so exempt from the board filter).
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
