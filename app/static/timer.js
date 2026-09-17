// The focus timer: a pomodoro timer in the corner of every page, with an
// optional card you're working on. Loaded after app.js (it uses its storage and
// flash helpers) and before Alpine.
//
// Everything here is client-side and per device. The state lives in
// localStorage, so it survives reloads and moving between pages, and other tabs
// follow along through the storage event. A running timer is saved as the time
// it ends, instead of a countdown, so it is robust to a tab being in the
// background or a laptop going to sleep.

const PHASE_NAMES = { focus: "Focus", short: "Short break", long: "Long break" };

const DEFAULT_SETTINGS = {
  focus: 25, // minutes
  short: 5,
  long: 15,
  every: 4, // a long break after this many focus sessions
  sound: true,
  volume: 0.6,
  confetti: true,
};

// The allowed range of each duration setting, as [min, max].
const DURATION_LIMITS = { focus: [1, 120], short: [1, 60], long: [1, 60], every: [2, 8] };

// A phase that ended longer ago than this, say while no tab was open, is shown
// as done without the sound and confetti.
const CELEBRATE_WITHIN_MS = 60 * 1000;

function defaultTimerState() {
  return {
    phase: "focus", // focus | short | long
    status: "idle", // idle | running | paused | done
    endsAt: null, // running: when the phase ends, in ms since the epoch
    remaining: null, // paused: ms left
    finished: null, // done: the phase that ended
    next: null, // done: the phase that comes next
    cycle: 0, // focus sessions finished or skipped, toward the next long break
    today: { date: "", count: 0 }, // focus sessions finished today
    focus: null, // the card being worked on, see cardInfo
    recents: [], // earlier focus cards, most recent first
    minimized: true,
  };
}

function readSaved(key, defaults) {
  let saved = {};
  try {
    saved = JSON.parse(storage.get(key)) ?? {};
  } catch {}
  // Only known keys, so a stale or hand-edited value can't add anything else.
  const result = { ...defaults };
  for (const name of Object.keys(defaults)) {
    if (name in saved) result[name] = saved[name];
  }
  return result;
}

function localDate() {
  return new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD
}

function formatTime(ms) {
  const seconds = Math.max(0, Math.ceil(ms / 1000));
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(Math.floor(seconds / 60))}:${pad(seconds % 60)}`;
}

// What the timer remembers about a card, read from its face on the board. The
// titles are kept so the dock can show the card from other pages.
function cardInfo(card) {
  const text = (el) => el.textContent.trim();
  return {
    cardId: Number(card.dataset.cardId),
    boardId: Number(document.getElementById("lists-container").dataset.boardId),
    title: text(card.querySelector(".card-title")),
    listTitle: text(card.closest(".list").querySelector(".list-title")),
    boardTitle: document.querySelector("#board-header .board-title").value,
  };
}

// ---- Sound ----------------------------------------------------------------

let ding = null;
let soundUnlocked = false;

function dingAudio() {
  ding ??= new Audio("/static/timer/ding.mp3");
  return ding;
}

function playDing(volume) {
  const audio = dingAudio();
  audio.muted = false;
  audio.volume = volume;
  audio.currentTime = 0;
  audio.play().catch(() => {}); // if the browser refuses, the done view still shows
}

// Some browsers (Safari) only play sound a page first started during a click.
// Starting a timer is a click, so play the sound silently then.
function unlockSound() {
  if (soundUnlocked) return;
  soundUnlocked = true;
  const audio = dingAudio();
  audio.muted = true;
  audio
    .play()
    .then(() => {
      audio.pause();
      audio.currentTime = 0;
    })
    .catch(() => (soundUnlocked = false))
    .finally(() => (audio.muted = false));
}

// ---- Confetti -------------------------------------------------------------

const CONFETTI = {
  particleCount: 120,
  spread: 75,
  startVelocity: 40,
  ticks: 220,
  colors: ["#c9372c", "#f87168", "#4bce97", "#f5cd47", "#579dff", "#9f8fef"],
  disableForReducedMotion: true,
};

function throwConfetti() {
  // An open card modal is drawn above everything else on the page, so confetti
  // on the page would be hidden behind it. Draw it inside the modal instead.
  const dialog = document.querySelector("#modal-root dialog[open]");
  if (dialog) {
    const canvas = document.createElement("canvas");
    canvas.className = "confetti-canvas";
    dialog.append(canvas);
    confetti.create(canvas, { resize: true, disableForReducedMotion: true })({
      ...CONFETTI,
      origin: { x: 0.5, y: 0.7 },
    });
    setTimeout(() => canvas.remove(), 8000);
    return;
  }

  // Up and to the left, out of the dock.
  const dock = document.getElementById("timer-dock").getBoundingClientRect();
  confetti({
    ...CONFETTI,
    angle: 115,
    zIndex: 1000,
    origin: {
      x: (dock.left + dock.width / 2) / window.innerWidth,
      y: dock.top / window.innerHeight,
    },
  });
}

// ---- State ----------------------------------------------------------------

let endTimeout = null; // fires when a running phase ends
let claimTimeout = null; // see phaseEnded

document.addEventListener("alpine:init", () => {
  Alpine.store("timer", {
    ...defaultTimerState(),
    settings: { ...DEFAULT_SETTINGS },
    now: Date.now(), // updated while running, so the time left re-renders
    picking: false, // choosing the focus card by clicking it (this tab only)

    init() {
      if (!document.getElementById("timer-dock")) return; // the login page
      this.load();

      const baseTitle = document.title;
      Alpine.effect(() => (document.title = this.titlePrefix + baseTitle));
      Alpine.effect(() => document.body.classList.toggle("timer-picking", this.picking));
      Alpine.effect(() => setFocusMark(this.focus?.cardId));

      // Ticks are slowed down in background tabs, so the end of a phase is
      // also caught by a timeout set for that moment (see scheduleEnd).
      setInterval(() => this.tick(), 250);
      document.addEventListener("visibilitychange", () => this.tick());
      window.addEventListener("storage", (event) => {
        if (event.key?.startsWith("kanbanano:timer")) this.load();
      });
      this.tick();
      syncFocusWithPage();
    },

    load() {
      Object.assign(this, readSaved("timer", defaultTimerState()));
      this.settings = readSaved("timer-settings", DEFAULT_SETTINGS);
      this.now = Date.now();
      this.scheduleEnd();
    },

    save() {
      const state = {};
      for (const name of Object.keys(defaultTimerState())) state[name] = this[name];
      storage.set("timer", JSON.stringify(state));
      this.scheduleEnd();
    },

    scheduleEnd() {
      clearTimeout(endTimeout);
      if (this.status === "running") {
        endTimeout = setTimeout(() => this.tick(), this.endsAt - Date.now());
      }
    },

    tick() {
      if (this.status !== "running") return;
      this.now = Date.now();
      if (this.now >= this.endsAt) this.phaseEnded();
    },

    // Every open tab notices the end. The first to mark the phase done plays the
    // sound, and the others load the done state instead. Browsers only let a tab
    // play sound after someone has used it, so used and visible tabs go first.
    phaseEnded() {
      if (claimTimeout) return;
      const endsAt = this.endsAt;
      let delay = Math.random() * 100;
      if (!navigator.userActivation?.hasBeenActive) delay += 400;
      if (document.hidden) delay += 200;

      claimTimeout = setTimeout(() => {
        claimTimeout = null;
        const saved = readSaved("timer", defaultTimerState());
        if (saved.status === "running" && saved.endsAt === endsAt) {
          this.finish(Date.now() - endsAt < CELEBRATE_WITHIN_MS);
        } else {
          this.load(); // another tab got there first
        }
      }, delay);
    },

    finish(celebrate) {
      const finished = this.phase;
      if (finished === "focus") {
        this.cycle++;
        const date = localDate();
        this.today = { date, count: this.today.date === date ? this.today.count + 1 : 1 };
      }
      Object.assign(this, {
        status: "done",
        finished,
        next: this.nextPhase(finished),
        endsAt: null,
        remaining: null,
        minimized: false,
      });
      this.save();

      if (!celebrate) return;
      if (this.settings.sound) playDing(this.settings.volume);
      if (finished === "focus" && this.settings.confetti) Alpine.nextTick(throwConfetti);
    },

    nextPhase(finished) {
      if (finished !== "focus") return "focus";
      return this.cycle % this.settings.every === 0 ? "long" : "short";
    },

    // ---- Actions ----

    setPhase(phase) {
      Object.assign(this, {
        phase,
        status: "idle",
        endsAt: null,
        remaining: null,
        finished: null,
        next: null,
      });
      this.save();
    },

    start() {
      unlockSound();
      const left = this.status === "paused" ? this.remaining : this.duration(this.phase);
      this.now = Date.now();
      Object.assign(this, { status: "running", endsAt: this.now + left, remaining: null });
      this.save();
    },

    pause() {
      Object.assign(this, {
        status: "paused",
        remaining: Math.max(0, this.endsAt - Date.now()),
        endsAt: null,
      });
      this.save();
    },

    // The play/pause button in the pill and the panel.
    toggle() {
      if (this.status === "running") this.pause();
      else if (this.status === "done") this.startNext();
      else this.start();
    },

    reset() {
      this.setPhase(this.phase);
    },

    // Ends the phase now without the sound or confetti. A skipped focus session
    // still counts toward the long break, but not toward today's count.
    skip() {
      if (this.phase === "focus") this.cycle++;
      this.setPhase(this.nextPhase(this.phase));
    },

    startNext() {
      this.setPhase(this.next);
      this.start();
    },

    notNow() {
      this.setPhase(this.next);
    },

    expand() {
      this.minimized = false;
      this.save();
    },

    minimize() {
      this.minimized = true;
      this.save();
    },

    // ---- Focus card ----

    // Doesn't start the timer. Starting it is always a separate click.
    setFocus(info) {
      const earlier = this.focus && this.focus.cardId !== info.cardId ? [this.focus] : [];
      this.recents = [...earlier, ...this.recents]
        .filter((card) => card.cardId !== info.cardId)
        .slice(0, 5);
      this.focus = info;
      this.picking = false;
      this.save();
    },

    clearFocus() {
      if (!this.focus) return;
      this.recents = [this.focus, ...this.recents].slice(0, 5);
      this.focus = null;
      this.save();
    },

    openFocusCard() {
      const { cardId, boardId } = this.focus;
      const card = document.getElementById(`card-${cardId}`);
      if (card) card.click(); // opens the modal, as clicking it on the board does
      else location.href = `/boards/${boardId}#card-${cardId}`;
    },

    // ---- Settings ----

    // Returns the value actually saved, so the field can show it.
    setDuration(name, value) {
      const [min, max] = DURATION_LIMITS[name];
      const number = Number.parseInt(value, 10);
      if (Number.isInteger(number)) this.settings[name] = Math.min(max, Math.max(min, number));
      storage.set("timer-settings", JSON.stringify(this.settings));
      return this.settings[name];
    },

    setSetting(name, value) {
      this.settings[name] = value;
      storage.set("timer-settings", JSON.stringify(this.settings));
    },

    testSound() {
      playDing(this.settings.volume);
    },

    testConfetti() {
      throwConfetti();
    },

    // ---- For the templates ----

    duration(phase) {
      return this.settings[phase] * 60 * 1000;
    },
    get running() {
      return this.status === "running";
    },
    get paused() {
      return this.status === "paused";
    },
    get done() {
      return this.status === "done";
    },
    get idle() {
      return this.status === "idle";
    },
    // Minimized with nothing going on: the dock is just a tomato.
    get quiet() {
      return this.idle && !this.focus;
    },
    get msLeft() {
      if (this.running) return Math.max(0, this.endsAt - this.now);
      if (this.paused) return this.remaining;
      if (this.done) return 0;
      return this.duration(this.phase);
    },
    get timeText() {
      return formatTime(this.msLeft);
    },
    get phaseName() {
      return PHASE_NAMES[this.phase];
    },
    get onBreak() {
      return this.phase !== "focus" && !this.done;
    },
    // Next to the time in the minimized pill. During a break it says so, so the
    // time left isn't mistaken for time left on the focus card.
    get pillLabel() {
      if (this.onBreak) return `Taking a ${this.phaseName.toLowerCase()}`;
      return this.focus ? this.focus.title : this.phaseName;
    },
    get phases() {
      return Object.entries(PHASE_NAMES).map(([id, name]) => ({ id, name }));
    },
    get toggleLabel() {
      if (this.running) return "Pause";
      if (this.paused) return "Resume";
      if (this.done) return `Start ${this.settings[this.next]} min ${PHASE_NAMES[this.next].toLowerCase()}`;
      return "Start";
    },
    get titlePrefix() {
      if (this.running) return `${this.timeText} · `;
      if (this.paused) return `‖ ${this.timeText} · `;
      if (this.done) return "Done · ";
      return "";
    },
    // The tomato drawing for the phase, switching between its two frames every
    // second while running.
    get tomatoSrc() {
      const frame = this.running && Math.floor(this.now / 1000) % 2 ? 2 : 1;
      return `/static/timer/${this.phase}${frame}.webp`;
    },
    get doneImage() {
      return `/static/timer/${this.next}1.webp`;
    },
    get todayCount() {
      return this.today.date === localDate() ? this.today.count : 0;
    },
    get doneHeading() {
      return this.finished === "focus" ? "Focus session done" : "Break's over";
    },
    get doneDetail() {
      if (this.finished === "focus") {
        const count = `${this.todayCount} today`;
        return this.focus ? `${this.focus.title} · ${count}` : count;
      }
      return this.focus ? `Back to: ${this.focus.title}` : "Ready for another focus session?";
    },
    // One dot per focus session in the cycle before a long break, filled for
    // those finished. During a break the session just finished counts, so a
    // long break shows them all filled.
    get dots() {
      const every = this.settings.every;
      let filled = this.cycle % every;
      if (filled === 0 && this.cycle > 0 && (this.phase !== "focus" || this.done)) filled = every;
      return Array.from({ length: every }, (_, i) => i < filled);
    },
    get cycleLabel() {
      if (this.phase === "focus") {
        return `Focus ${(this.cycle % this.settings.every) + 1} of ${this.settings.every}`;
      }
      return `Taking a ${this.phaseName.toLowerCase()}`;
    },
    get focusHeading() {
      return this.phase === "focus" ? "Focusing on" : "Up next";
    },
  });

  // ---- The dock -----------------------------------------------------------
  // The panel's own state: whether settings are showing, and whether the
  // "Change" menu is open.

  Alpine.data("timerDock", () => ({
    settingsOpen: false,
    changeOpen: false,
    onBoard: document.getElementById("lists-container") !== null,

    get timer() {
      return this.$store.timer;
    },

    openSettings() {
      this.changeOpen = false;
      this.settingsOpen = true;
    },

    minimize() {
      this.settingsOpen = false;
      this.changeOpen = false;
      this.timer.minimize();
    },

    startPicking() {
      this.changeOpen = false;
      this.timer.picking = true;
    },
  }));

  // ---- "Focus on this" in the card modal ----------------------------------

  Alpine.data("focusButton", (cardId) => ({
    get isFocus() {
      return this.$store.timer.focus?.cardId === cardId;
    },

    focus() {
      const card = document.getElementById(`card-${cardId}`);
      if (card) this.$store.timer.setFocus(cardInfo(card));
    },
  }));
});

// ---- Marking the focus card -----------------------------------------------
// A generated CSS rule, like the board filter, so it also applies to
// cards a board refresh re-renders. The tag is styled in app.css. The outline is
// inset, like .card:focus-visible, so the list's scroll container doesn't clip it.
// The focus card stays visible when that filter is on.

const focusMarkStyle = document.createElement("style");
document.head.append(focusMarkStyle);

function setFocusMark(cardId) {
  const id = Number.parseInt(cardId, 10); // interpolated into CSS: keep it a number
  focusMarkStyle.textContent = Number.isInteger(id)
    ? `.card[data-card-id="${id}"] { --focus-tag: "🍅 Focus"; box-shadow: inset 0 0 0 2px #c9372c, 0 1px 1px rgb(9 30 66 / 0.25); display: list-item !important; }`
    : "";
}

// ---- Picking a card on the board ------------------------------------------
// While picking, a click (or Enter) on a card makes it the focus instead of
// opening it. Capturing on the document runs this before htmx sees the event.

function pickCard(event) {
  const timer = Alpine.store("timer");
  const card = event.target.closest(".card");
  if (!timer.picking || !card) return;
  event.preventDefault();
  event.stopPropagation();
  timer.setFocus(cardInfo(card));
}

document.addEventListener("click", pickCard, true);
document.addEventListener(
  "keydown",
  (event) => {
    if (event.key === "Enter") pickCard(event);
    else if (event.key === "Escape" && Alpine.store("timer").picking) Alpine.store("timer").picking = false;
  },
  true,
);

// ---- Keeping the focus card up to date ------------------------------------
// The saved titles are refreshed from the board on screen, and cards deleted
// since are forgotten. Card ids are unique across boards, so a card missing from
// its own board is gone. On the board index, the same goes for deleted boards.

function syncFocusWithPage() {
  const timer = Alpine.store("timer");
  const container = document.getElementById("lists-container");
  const boardId = Number(container?.dataset.boardId);
  const tiles = document.querySelector(".board-tiles");

  // Returns the card's fresh info, or null if it's been deleted. Cards on
  // another board can't be checked from here, so they're returned unchanged.
  const check = (info) => {
    if (tiles) return tiles.querySelector(`a[href="/boards/${info.boardId}"]`) ? info : null;
    if (info.boardId !== boardId) return info;
    const card = document.getElementById(`card-${info.cardId}`);
    return card ? cardInfo(card) : null;
  };
  if (!container?.dataset.boardId && !tiles) return; // login, or a deleted board

  const before = JSON.stringify([timer.focus, timer.recents]);
  if (timer.focus) {
    const fresh = check(timer.focus);
    if (!fresh) flash(tiles ? "The board you were focusing on was deleted." : "The card you were focusing on was deleted.");
    timer.focus = fresh;
  }
  timer.recents = timer.recents.map(check).filter(Boolean);
  if (JSON.stringify([timer.focus, timer.recents]) !== before) timer.save();
}

// After board refreshes and our own changes. A deleted card is removed by a
// swap on the card itself, whose events don't reach the document once it's
// gone, so afterRequest (from the delete button) is watched too.
document.addEventListener("htmx:afterSettle", () => setTimeout(syncFocusWithPage));
document.addEventListener("htmx:afterRequest", () => setTimeout(syncFocusWithPage));
