import json
import os
import re
import secrets
import sqlite3
from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import db

BASE_DIR = Path(__file__).parent


def required_env(name: str) -> str:
    """An environment variable the app can't run without. Fails at startup, loudly,
    rather than on the first login attempt."""
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"The {name} environment variable must be set")
    return value


# One shared password for everyone. There are no user accounts: "who am I" is a
# per-device preference in the header, not something the server knows.
BOARD_PASSWORD = required_env("BOARD_PASSWORD")
# Signs the session cookie. Changing it logs every device out.
SECRET_KEY = required_env("SECRET_KEY")
# The session cookie is Secure by default, since Tailscale Serve terminates TLS
# in front. Browsers won't send a Secure cookie over plain HTTP (localhost aside),
# so reaching the app by LAN IP without TLS would loop back to the login page
# forever. ALLOW_HTTP=1 drops the flag for that setup.
ALLOW_HTTP = os.environ.get("ALLOW_HTTP") == "1"

SESSION_MAX_AGE = 365 * 24 * 60 * 60  # typed once per device

# Paths reachable without logging in. Static files aren't listed because they're
# a mount, which app-wide dependencies never reach, so they're always open: the
# login page needs its stylesheet.
LOGIN_EXEMPT = {"/login", "/logout"}


def require_login(request: Request) -> None:
    """App-wide dependency: turn away anyone without a logged-in session.

    Applied to the whole app rather than route by route, so a new route is
    protected unless it's deliberately added to LOGIN_EXEMPT.
    """
    if request.url.path in LOGIN_EXEMPT or request.session.get("logged_in"):
        return
    if "HX-Request" in request.headers:
        # An ordinary redirect would be followed inside the XHR, and the login
        # page swapped into whatever the request was targeting. HX-Redirect makes
        # htmx send the whole page there instead. This is also how an open board
        # notices, on its next poll, that the session has gone.
        raise HTTPException(
            status_code=401, detail="Please log in again", headers={"HX-Redirect": "/login"}
        )
    if request.method == "GET":
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    raise HTTPException(status_code=401, detail="Please log in again")


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    try:
        db.init_db(conn)
    finally:
        conn.close()
    yield


# No /docs or /openapi.json: those are added outside the dependency system, so
# require_login wouldn't cover them, and nothing here needs them.
app = FastAPI(
    lifespan=lifespan,
    dependencies=[Depends(require_login)],
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.middleware("http")
async def revalidate_static_files(request: Request, call_next):
    """Make browsers check static files for changes on every load.

    Without a Cache-Control header they may reuse a cached copy for a while
    without asking, so a page can keep running old JavaScript after an update.
    Checking costs a 304 when nothing changed.
    """
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.middleware("http")
async def report_board_version(request: Request, call_next):
    """Tell the client which board version its own change produced.

    Every write bumps the board version, so without this the client's next poll
    would see a new version and refetch the board just to show a change it's
    already showing. See adoptVersion in app.js for how the client uses it.
    """
    response = await call_next(request)
    conn = getattr(request.state, "db", None)
    if conn is not None and conn.board_version is not None:
        response.headers["X-Board-Version"] = str(conn.board_version)
    return response


# Added after the middleware above, so it wraps them and the session is decoded
# before anything else looks at the request. The cookie is signed, not encrypted:
# it holds nothing but the logged-in flag. Starlette re-issues it on every
# response, so the year runs from a device's last visit, not its login.
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    max_age=SESSION_MAX_AGE,
    same_site="lax",
    https_only=not ALLOW_HTTP,
)


def initials(name: str) -> str:
    """'Alex Smith' -> 'AS', 'Sam' -> 'S'."""
    return "".join(word[0] for word in name.split()[:2]).upper()


templates.env.filters["initials"] = initials
templates.env.globals["LABEL_COLORS"] = db.LABEL_COLORS
templates.env.globals["BOARD_COLORS"] = db.BOARD_COLORS


def clean_text(value: str, what: str) -> str:
    """Titles and names are one line: collapse pasted newlines and runs of spaces.

    400 if nothing is left; `what` names the field in the message the user sees.
    """
    value = " ".join(value.split())
    if not value:
        raise HTTPException(status_code=400, detail=f"{what} can't be empty")
    return value


def attach_labels(conn: sqlite3.Connection, cards: list[dict]) -> list[dict]:
    """Give each card dict `labels` and `people` lists, in creation order. One query."""
    by_id = {card["id"]: card for card in cards}
    for card in cards:
        card["labels"], card["people"] = [], []
    for row in conn.execute(
        """
        SELECT card_labels.card_id, labels.id, labels.name, labels.color, labels.kind
        FROM card_labels JOIN labels ON labels.id = card_labels.label_id
        WHERE card_labels.card_id IN (SELECT value FROM json_each(?))
        ORDER BY labels.id
        """,
        (json.dumps(list(by_id)),),
    ):
        by_id[row["card_id"]]["people" if row["kind"] == "person" else "labels"].append(row)
    return cards


def attach_progress(conn: sqlite3.Connection, cards: list[dict]) -> list[dict]:
    """Give each card dict `checklist_done` and `checklist_total`. One query.

    Counts, not the items themselves: this is what the card face shows. The
    modal loads the full checklist separately with load_checklist.
    """
    by_id = {card["id"]: card for card in cards}
    for card in cards:
        card["checklist_done"], card["checklist_total"] = 0, 0
    for row in conn.execute(
        """
        SELECT card_id, SUM(done) AS done, COUNT(*) AS total
        FROM checklist_items
        WHERE card_id IN (SELECT value FROM json_each(?))
        GROUP BY card_id
        """,
        (json.dumps(list(by_id)),),
    ):
        by_id[row["card_id"]]["checklist_done"] = row["done"]
        by_id[row["card_id"]]["checklist_total"] = row["total"]
    return cards


def load_checklist(conn: sqlite3.Connection, card_id: int) -> list[sqlite3.Row]:
    """A card's checklist items, in order."""
    return conn.execute(
        "SELECT * FROM checklist_items WHERE card_id = ? ORDER BY position, id", (card_id,)
    ).fetchall()


def load_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row:
    """One checklist item, with the board it hangs off for the version bump."""
    item = conn.execute(
        """
        SELECT checklist_items.*, lists.board_id
        FROM checklist_items
        JOIN cards ON cards.id = checklist_items.card_id
        JOIN lists ON lists.id = cards.list_id
        WHERE checklist_items.id = ?
        """,
        (item_id,),
    ).fetchone()
    if item is None:
        raise HTTPException(status_code=404, detail="This checklist item was deleted")
    return item


def load_board(conn: sqlite3.Connection, board_id: int) -> sqlite3.Row:
    """One board. 404 if it's been deleted, which also covers a made-up URL."""
    board = conn.execute("SELECT * FROM boards WHERE id = ?", (board_id,)).fetchone()
    if board is None:
        raise HTTPException(status_code=404, detail="This board was deleted")
    return board


def load_list(conn: sqlite3.Connection, list_id: int) -> sqlite3.Row:
    """One list. 404 if it's been deleted."""
    lst = conn.execute("SELECT * FROM lists WHERE id = ?", (list_id,)).fetchone()
    if lst is None:
        raise HTTPException(status_code=404, detail="This list was deleted")
    return lst


def load_lists(conn: sqlite3.Connection, board_id: int) -> list[dict]:
    """A board's lists in order, each with its cards in order. Four queries total."""
    lists = [
        dict(row)
        for row in conn.execute(
            "SELECT id, title FROM lists WHERE board_id = ? ORDER BY position, id",
            (board_id,),
        )
    ]
    cards = [
        dict(row)
        for row in conn.execute(
            """
            SELECT cards.id, cards.list_id, cards.title, cards.description != '' AS has_description
            FROM cards JOIN lists ON lists.id = cards.list_id
            WHERE lists.board_id = ?
            ORDER BY cards.position, cards.id
            """,
            (board_id,),
        )
    ]
    attach_labels(conn, cards)
    attach_progress(conn, cards)
    cards_by_list = {lst["id"]: [] for lst in lists}
    for card in cards:
        cards_by_list[card["list_id"]].append(card)
    for lst in lists:
        lst["cards"] = cards_by_list[lst["id"]]
    return lists


def load_cards(conn: sqlite3.Connection, card_ids: Iterable[int]) -> list[dict]:
    """Cards with everything their face and modal need. Deleted ids are skipped."""
    rows = conn.execute(
        """
        SELECT cards.*, cards.description != '' AS has_description,
               lists.board_id, lists.title AS list_title
        FROM cards JOIN lists ON lists.id = cards.list_id
        WHERE cards.id IN (SELECT value FROM json_each(?))
        """,
        (json.dumps(list(card_ids)),),
    )
    cards = [dict(row) for row in rows]
    attach_labels(conn, cards)
    return attach_progress(conn, cards)


def load_card(conn: sqlite3.Connection, card_id: int) -> dict:
    """One card, as load_cards. 404 if it's been deleted."""
    cards = load_cards(conn, [card_id])
    if not cards:
        raise HTTPException(status_code=404, detail="This card was deleted")
    return cards[0]


def board_labels(conn: sqlite3.Connection, board_id: int) -> dict[str, list]:
    """All of a board's labels, split by kind: {"label": [...], "person": [...]}."""
    options = {"label": [], "person": []}
    for row in conn.execute(
        "SELECT id, name, color, kind FROM labels WHERE board_id = ? ORDER BY id", (board_id,)
    ):
        options[row["kind"]].append(row)
    return options


def check_color(color: str) -> None:
    # Label colors are rendered into style attributes, so this is also what
    # keeps arbitrary CSS out of the page.
    if color not in db.LABEL_COLORS.values():
        raise HTTPException(status_code=400, detail="Pick a color from the palette")


HEX_COLOR = re.compile(r"#[0-9a-f]{6}", re.IGNORECASE)


def clean_background(color: str) -> str:
    """A board background: any #rrggbb color, lower-cased. 400 for anything else.

    Deliberately strict. The value is written into a <style> element, where
    Jinja's autoescape is no protection: `red; } body { display: none }` has no
    HTML-special characters to escape. Six hex digits admit no way out of it.
    """
    if not HEX_COLOR.fullmatch(color):
        raise HTTPException(status_code=400, detail="Pick a color")
    return color.lower()


def relative_luminance(color: str) -> float:
    """WCAG relative luminance of a #rrggbb color: 0 for black, 1 for white."""

    def linear(channel: int) -> float:
        c = channel / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)


def needs_dark_ink(background: str) -> bool:
    """Whether text on this background should be dark rather than white.

    White unless it falls below 3:1, WCAG's floor for large text and controls,
    which is what sits on a board's background. Not simply "whichever contrasts
    more": for mid-tones neither reaches 4.5:1, and that rule would flip half
    the presets to dark text over margins as thin as 3.75 vs 3.76.
    """
    contrast_with_white = 1.05 / (relative_luminance(background) + 0.05)
    return contrast_with_white < 3


templates.env.filters["needs_dark_ink"] = needs_dark_ink


def get_label(conn: sqlite3.Connection, label_id: int, board_id: int) -> sqlite3.Row:
    label = conn.execute(
        "SELECT id, kind FROM labels WHERE id = ? AND board_id = ?", (label_id, board_id)
    ).fetchone()
    if label is None:
        raise HTTPException(status_code=404, detail="This label was deleted")
    return label


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("logged_in"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html")


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, password: Annotated[str, Form()] = ""):
    """Check the shared password and start a session.

    A plain form post rather than htmx: it's the one page that has to work
    before anything else does. compare_digest so the time taken doesn't hint
    at how much of a guess was right.
    """
    if not secrets.compare_digest(password.encode(), BOARD_PASSWORD.encode()):
        return templates.TemplateResponse(
            request, "login.html", {"error": "That's not the password."}, status_code=401
        )
    request.session["logged_in"] = True
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def board_index(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    boards = conn.execute(
        "SELECT id, title, background FROM boards ORDER BY created_at, id"
    ).fetchall()
    return templates.TemplateResponse(request, "index.html", {"boards": boards})


@app.post("/boards")
def create_board(
    title: Annotated[str, Form()], conn: sqlite3.Connection = Depends(db.get_db)
):
    """Create an empty board and send the browser straight to it.

    HX-Redirect rather than a 303 so a bad title comes back as a flash on the
    index page like every other error, instead of a raw error page.
    """
    title = clean_text(title, "Board title")
    with conn:
        board_id = conn.execute("INSERT INTO boards (title) VALUES (?)", (title,)).lastrowid
    return Response(status_code=204, headers={"HX-Redirect": f"/boards/{board_id}"})


@app.get("/boards/{board_id}", response_class=HTMLResponse)
def board_view(
    board_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)
):
    return templates.TemplateResponse(
        request,
        "board.html",
        {
            "board": load_board(conn, board_id),
            "lists": load_lists(conn, board_id),
            "people": board_labels(conn, board_id)["person"],
        },
    )


@app.patch("/boards/{board_id}", response_class=HTMLResponse)
def update_board(
    board_id: int,
    request: Request,
    title: Annotated[str | None, Form()] = None,
    background: Annotated[str | None, Form()] = None,
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Rename a board, or set its background. Send whichever one changed.

    The two answer with different fragments because they change different
    things: a rename has to send back the header, so the title field's value
    attribute — what Escape reverts to — stays current, the same reason a list's
    does (see _list_header.html). A background only has to send back the menu,
    which keeps it open for the next colour.
    """
    if title is not None:
        title = clean_text(title, "Board title")
    if background is not None:
        background = clean_background(background)

    with conn:
        board = load_board(conn, board_id)
        conn.execute(
            "UPDATE boards SET title = ?, background = ? WHERE id = ?",
            (
                board["title"] if title is None else title,
                board["background"] if background is None else background,
                board_id,
            ),
        )
        db.bump_version(conn, board_id)

    board = load_board(conn, board_id)
    if background is not None:
        return templates.TemplateResponse(request, "_board_menu.html", {"board": board})
    return templates.TemplateResponse(
        request,
        "_board_header.html",
        {"board": board, "people": board_labels(conn, board_id)["person"]},
    )


@app.delete("/boards/{board_id}")
def delete_board(board_id: int, conn: sqlite3.Connection = Depends(db.get_db)):
    """Delete a board and everything on it, then go back to the index.

    No version bump: the row is gone, so other clients' polls get the 286 that
    tells them the board is no longer there.
    """
    with conn:
        load_board(conn, board_id)
        conn.execute("DELETE FROM boards WHERE id = ?", (board_id,))
    return Response(status_code=204, headers={"HX-Redirect": "/"})


@app.get("/boards/{board_id}/poll", response_class=HTMLResponse)
def poll_board(
    board_id: int, v: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)
):
    """204 if version `v` is current, otherwise the freshly rendered lists container.

    The version is read before the lists, so a write landing in between makes
    the content newer than its label, never older: at worst the next poll
    fetches again, rather than missing a change.
    """
    board = conn.execute("SELECT * FROM boards WHERE id = ?", (board_id,)).fetchone()
    if board is None:
        # 286 tells htmx to stop polling; the message replaces the lists.
        return HTMLResponse(
            '<div id="lists-container" class="lists board-gone">This board was deleted.</div>',
            status_code=286,
        )
    if board["version"] == v:
        return Response(status_code=204)
    return templates.TemplateResponse(
        request,
        "_board_refresh.html",
        {
            "board": board,
            "lists": load_lists(conn, board_id),
            "people": board_labels(conn, board_id)["person"],
        },
    )


@app.post("/lists", response_class=HTMLResponse)
def create_list(
    request: Request,
    board_id: Annotated[int, Form()],
    title: Annotated[str, Form()],
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Append a list to the right-hand end of a board. Returns just the new list."""
    title = clean_text(title, "List title")
    with conn:
        load_board(conn, board_id)
        list_id = conn.execute(
            """
            INSERT INTO lists (board_id, title, position)
            VALUES (?, ?, (SELECT COALESCE(MAX(position) + 1, 0) FROM lists WHERE board_id = ?))
            """,
            (board_id, title, board_id),
        ).lastrowid
        db.bump_version(conn, board_id)

    lst = dict(load_list(conn, list_id), cards=[])
    return templates.TemplateResponse(request, "_list.html", {"list": lst})


@app.patch("/lists/{list_id}", response_class=HTMLResponse)
def update_list(
    list_id: int,
    request: Request,
    title: Annotated[str, Form()],
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Rename a list. Returns the list's header, which replaces itself."""
    title = clean_text(title, "List title")
    with conn:
        lst = load_list(conn, list_id)
        conn.execute("UPDATE lists SET title = ? WHERE id = ?", (title, list_id))
        db.bump_version(conn, lst["board_id"])
    return templates.TemplateResponse(
        request, "_list_header.html", {"list": load_list(conn, list_id)}
    )


@app.delete("/lists/{list_id}", response_class=HTMLResponse)
def delete_list(list_id: int, conn: sqlite3.Connection = Depends(db.get_db)):
    """Delete a list and its cards. Empty 200 for the same reason as delete_card."""
    with conn:
        lst = load_list(conn, list_id)
        conn.execute("DELETE FROM lists WHERE id = ?", (list_id,))
        db.bump_version(conn, lst["board_id"])
    return HTMLResponse("")


@app.post("/cards", response_class=HTMLResponse)
def create_card(
    request: Request,
    list_id: Annotated[int, Form()],
    title: Annotated[str, Form()],
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Append a card to the bottom of a list. Returns just the new card's HTML."""
    title = clean_text(title, "Card title")
    with conn:
        lst = load_list(conn, list_id)
        card_id = conn.execute(
            """
            INSERT INTO cards (list_id, title, position)
            VALUES (?, ?, (SELECT COALESCE(MAX(position) + 1, 0) FROM cards WHERE list_id = ?))
            """,
            (list_id, title, list_id),
        ).lastrowid
        db.bump_version(conn, lst["board_id"])

    return templates.TemplateResponse(request, "_card.html", {"card": load_card(conn, card_id)})


def rewrite_positions(
    conn: sqlite3.Connection,
    table: str,
    parent_column: str,
    parent_id: int,
    ordered_ids: list[int],
    exclude: Iterable[int] = (),
) -> None:
    """Renumber a parent's children 0, 1, 2…, with `ordered_ids` first, in that order.

    `ordered_ids` are moved under the parent if they aren't there already; the
    caller must have checked they belong to the same board. Children the client
    didn't know about (e.g. added by someone else since its page loaded) follow
    in their existing order, so positions stay contiguous. `exclude` holds ids
    being placed under a different parent in the same request.

    All position writes for drags go through here. Moves deliberately leave
    cards.updated_at alone: it's the conflict token for description edits.
    """
    skip = {*ordered_ids, *exclude}
    leftovers = [
        row["id"]
        for row in conn.execute(
            f"SELECT id FROM {table} WHERE {parent_column} = ? ORDER BY position, id",
            (parent_id,),
        )
        if row["id"] not in skip
    ]
    conn.executemany(
        f"UPDATE {table} SET {parent_column} = ?, position = ? WHERE id = ?",
        [(parent_id, position, id_) for position, id_ in enumerate(ordered_ids + leftovers)],
    )


@app.post("/cards/reorder", status_code=204)
def reorder_cards(
    list_id: Annotated[int, Form()],
    card_ids: Annotated[list[int], Form()],
    from_list_id: Annotated[int | None, Form()] = None,
    from_card_ids: Annotated[list[int], Form()] = [],
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Save card order after a drag.

    Takes the full card order of the list the card was dropped in and, for a
    move between lists, the full order of the list it left (possibly empty).
    """
    orders = {list_id: card_ids}
    if from_list_id is not None and from_list_id != list_id:
        orders[from_list_id] = from_card_ids

    with conn:
        lists = conn.execute(
            "SELECT id, board_id FROM lists WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(list(orders)),),
        ).fetchall()
        if len(lists) < len(orders):
            raise HTTPException(status_code=404, detail="This list was deleted")
        board_ids = {lst["board_id"] for lst in lists}
        if len(board_ids) > 1:
            raise HTTPException(status_code=400, detail="Can't move cards between boards")
        board_id = board_ids.pop()

        # Drop ids for cards deleted since the page loaded, or from other boards.
        on_board = {
            row["id"]
            for row in conn.execute(
                """
                SELECT cards.id FROM cards JOIN lists ON lists.id = cards.list_id
                WHERE lists.board_id = ? AND cards.id IN (SELECT value FROM json_each(?))
                """,
                (board_id, json.dumps(card_ids + from_card_ids)),
            )
        }
        in_request = set(card_ids) | set(from_card_ids)
        for target_list_id, ids in orders.items():
            known = [id_ for id_ in dict.fromkeys(ids) if id_ in on_board]
            rewrite_positions(conn, "cards", "list_id", target_list_id, known, in_request)
        db.bump_version(conn, board_id)


@app.post("/lists/reorder", status_code=204)
def reorder_lists(
    board_id: Annotated[int, Form()],
    list_ids: Annotated[list[int], Form()],
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Save list order after a drag. Takes the board's full list order."""
    with conn:
        load_board(conn, board_id)
        on_board = {
            row["id"]
            for row in conn.execute(
                "SELECT id FROM lists WHERE board_id = ? AND id IN (SELECT value FROM json_each(?))",
                (board_id, json.dumps(list_ids)),
            )
        }
        known = [id_ for id_ in dict.fromkeys(list_ids) if id_ in on_board]
        rewrite_positions(conn, "lists", "board_id", board_id, known)
        db.bump_version(conn, board_id)


@app.get("/cards/{card_id}", response_class=HTMLResponse)
def card_detail(card_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    """The card modal: a <dialog> that opens itself once swapped into #modal-root."""
    card = load_card(conn, card_id)
    return templates.TemplateResponse(
        request,
        "_card_modal.html",
        {
            "card": card,
            "options": board_labels(conn, card["board_id"]),
            "items": load_checklist(conn, card_id),
        },
    )


@app.patch("/cards/{card_id}", response_class=HTMLResponse)
def update_card(
    card_id: int,
    request: Request,
    updated_at: Annotated[str, Form()],
    title: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Save the modal's title and description.

    `updated_at` is the value the modal was loaded with. If the card has been
    saved since, reject with 409 rather than silently overwrite that edit.
    Returns the new card face and updated_at, both swapped out-of-band.
    """
    title = clean_text(title, "Card title")
    description = "\n".join(description.splitlines()).strip()  # browsers send \r\n
    with conn:
        card = load_card(conn, card_id)
        if card["updated_at"] != updated_at:
            raise HTTPException(
                status_code=409,
                detail="Someone else changed this card after you opened it. "
                "Reload it to see their version.",
            )
        conn.execute(
            """
            UPDATE cards
            SET title = ?, description = ?, updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now')
            WHERE id = ?
            """,
            (title, description, card_id),
        )
        db.bump_version(conn, card["board_id"])
    return templates.TemplateResponse(
        request, "_card_saved.html", {"card": load_card(conn, card_id)}
    )


@app.delete("/cards/{card_id}", response_class=HTMLResponse)
def delete_card(card_id: int, conn: sqlite3.Connection = Depends(db.get_db)):
    """Delete a card.

    Responds 200 with an empty body, not 204: htmx skips the swap on a 204, and
    the swap is what removes the card from the board. The gap this leaves in the
    list's positions is harmless; the next drag in that list renumbers it.
    """
    with conn:
        card = load_card(conn, card_id)
        conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
        db.bump_version(conn, card["board_id"])
    return HTMLResponse("")


def label_section(
    request: Request,
    conn: sqlite3.Connection,
    card_id: int,
    kind: str,
    face_ids: Iterable[int],
    *,
    managing: bool = False,
    people_changed: bool = False,
):
    """Re-render the modal's labels or people section after a change.

    Also swaps out-of-band the faces of `face_ids` (every card showing what
    changed) and, if the set of people changed, the header's "I am" picker.
    `managing` keeps the section in its edit view.
    """
    card = load_card(conn, card_id)
    return templates.TemplateResponse(
        request,
        "_label_section.html",
        {
            "card": card,
            "kind": kind,
            "options": board_labels(conn, card["board_id"])[kind],
            "managing": managing,
            "faces": load_cards(conn, face_ids),
            "refresh_identity": kind == "person" and people_changed,
        },
    )


@app.post("/cards/{card_id}/labels", response_class=HTMLResponse)
def add_card_label(
    card_id: int,
    request: Request,
    label_id: Annotated[int, Form()],
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Assign a label or person to a card. Idempotent, so a stale modal can't
    accidentally undo someone else's change the way a toggle could."""
    with conn:
        card = load_card(conn, card_id)
        label = get_label(conn, label_id, card["board_id"])
        conn.execute(
            "INSERT OR IGNORE INTO card_labels (card_id, label_id) VALUES (?, ?)",
            (card_id, label_id),
        )
        db.bump_version(conn, card["board_id"])
    return label_section(request, conn, card_id, label["kind"], [card_id])


@app.delete("/cards/{card_id}/labels/{label_id}", response_class=HTMLResponse)
def remove_card_label(
    card_id: int, label_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)
):
    """Unassign a label or person from a card. Idempotent."""
    with conn:
        card = load_card(conn, card_id)
        label = get_label(conn, label_id, card["board_id"])
        conn.execute(
            "DELETE FROM card_labels WHERE card_id = ? AND label_id = ?", (card_id, label_id)
        )
        db.bump_version(conn, card["board_id"])
    return label_section(request, conn, card_id, label["kind"], [card_id])


@app.post("/labels", response_class=HTMLResponse)
def create_label(
    request: Request,
    card_id: Annotated[int, Form()],
    kind: Annotated[Literal["label", "person"], Form()],
    name: Annotated[str, Form()] = "",
    color: Annotated[str, Form()] = "",
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Create a label or person on a card's board and assign it to that card.

    Takes a card rather than a board because labels are created from the card
    modal. Labels pick a palette color; people get the next one automatically.
    """
    name = clean_text(name, "Name")
    if kind == "label":
        check_color(color)

    with conn:
        card = load_card(conn, card_id)
        if kind == "person":
            (people,) = conn.execute(
                "SELECT COUNT(*) FROM labels WHERE board_id = ? AND kind = 'person'",
                (card["board_id"],),
            ).fetchone()
            palette = list(db.LABEL_COLORS.values())
            color = palette[people % len(palette)]
        label_id = conn.execute(
            "INSERT INTO labels (board_id, name, color, kind) VALUES (?, ?, ?, ?)",
            (card["board_id"], name, color, kind),
        ).lastrowid
        conn.execute(
            "INSERT INTO card_labels (card_id, label_id) VALUES (?, ?)", (card_id, label_id)
        )
        db.bump_version(conn, card["board_id"])
    return label_section(request, conn, card_id, kind, [card_id], people_changed=True)


def cards_with_label(conn: sqlite3.Connection, label_id: int) -> list[int]:
    return [
        row["card_id"]
        for row in conn.execute("SELECT card_id FROM card_labels WHERE label_id = ?", (label_id,))
    ]


@app.patch("/labels/{label_id}", response_class=HTMLResponse)
def update_label(
    label_id: int,
    request: Request,
    card_id: Annotated[int, Form()],
    name: Annotated[str, Form()] = "",
    color: Annotated[str, Form()] = "",
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Rename or recolor a label or person.

    Like POST /labels this is done from a card's modal, so it takes the card:
    the response is that modal's section, plus every card face showing the label.
    """
    name = clean_text(name, "Name")
    check_color(color)
    with conn:
        card = load_card(conn, card_id)
        label = get_label(conn, label_id, card["board_id"])
        conn.execute("UPDATE labels SET name = ?, color = ? WHERE id = ?", (name, color, label_id))
        db.bump_version(conn, card["board_id"])
    return label_section(
        request,
        conn,
        card_id,
        label["kind"],
        cards_with_label(conn, label_id),
        managing=True,
        people_changed=True,
    )


@app.delete("/labels/{label_id}", response_class=HTMLResponse)
def delete_label(
    label_id: int, card_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)
):
    """Delete a label or person, removing it from every card. `card_id` (a query
    parameter) is the card whose modal this was done from, as for PATCH."""
    with conn:
        card = load_card(conn, card_id)
        label = get_label(conn, label_id, card["board_id"])
        affected = cards_with_label(conn, label_id)  # read before the cascade clears them
        conn.execute("DELETE FROM labels WHERE id = ?", (label_id,))
        db.bump_version(conn, card["board_id"])
    return label_section(
        request, conn, card_id, label["kind"], affected, managing=True, people_changed=True
    )


def checklist_body(request: Request, conn: sqlite3.Connection, card_id: int):
    """Re-render the modal's checklist after a change.

    Every checklist write answers with this: the progress count and the items,
    plus the card's face out-of-band, since the face shows the same count. The
    add-item form sits outside this block, so it keeps focus and whatever is
    half-typed in it.
    """
    return templates.TemplateResponse(
        request,
        "_checklist_saved.html",
        {"card": load_card(conn, card_id), "items": load_checklist(conn, card_id)},
    )


@app.post("/cards/{card_id}/checklist", response_class=HTMLResponse)
def add_checklist_item(
    card_id: int,
    request: Request,
    text: Annotated[str, Form()],
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Append an item to a card's checklist."""
    text = clean_text(text, "Checklist item")
    with conn:
        card = load_card(conn, card_id)
        conn.execute(
            """
            INSERT INTO checklist_items (card_id, text, position)
            VALUES (?, ?, (SELECT COALESCE(MAX(position) + 1, 0) FROM checklist_items
                           WHERE card_id = ?))
            """,
            (card_id, text, card_id),
        )
        db.bump_version(conn, card["board_id"])
    return checklist_body(request, conn, card_id)


@app.patch("/checklist/{item_id}", response_class=HTMLResponse)
def update_checklist_item(
    item_id: int,
    request: Request,
    done: Annotated[bool | None, Form()] = None,
    text: Annotated[str | None, Form()] = None,
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Tick an item off, or edit its text. Send whichever one changed.

    `done` is sent as the value to set rather than as a toggle, so a stale modal
    ticking a box can't untick what someone else just ticked — the same reason
    the label endpoints are idempotent.
    """
    if text is not None:
        text = clean_text(text, "Checklist item")
    with conn:
        item = load_item(conn, item_id)
        conn.execute(
            "UPDATE checklist_items SET done = ?, text = ? WHERE id = ?",
            (
                item["done"] if done is None else done,
                item["text"] if text is None else text,
                item_id,
            ),
        )
        db.bump_version(conn, item["board_id"])
    return checklist_body(request, conn, item["card_id"])


@app.delete("/checklist/{item_id}", response_class=HTMLResponse)
def delete_checklist_item(
    item_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)
):
    """Delete a checklist item. The gap left in positions is harmless, as for cards."""
    with conn:
        item = load_item(conn, item_id)
        conn.execute("DELETE FROM checklist_items WHERE id = ?", (item_id,))
        db.bump_version(conn, item["board_id"])
    return checklist_body(request, conn, item["card_id"])
