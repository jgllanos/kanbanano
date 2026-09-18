import io
import json
import os
import re
import secrets
import sqlite3
from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageOps
from starlette.middleware.sessions import SessionMiddleware

from app import db, markdown

BASE_DIR = Path(__file__).parent


def required_env(name: str) -> str:
    """Read a required environment variable, failing at startup if it's unset."""
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"The {name} environment variable must be set")
    return value


# The shared password everyone logs in with. There are no user accounts.
BOARD_PASSWORD = required_env("BOARD_PASSWORD")
# Signs the session cookie. Changing it logs every device out.
SECRET_KEY = required_env("SECRET_KEY")
# The session cookie is marked Secure, and browsers only send those back over
# HTTPS or to localhost. ALLOW_HTTP=1 turns that off for serving over plain HTTP.
ALLOW_HTTP = os.environ.get("ALLOW_HTTP") == "1"

SESSION_MAX_AGE = 365 * 24 * 60 * 60  # one year

# Paths that don't need a login. Static files are always public: they're a
# mount, and app-wide dependencies don't apply to mounts.
LOGIN_EXEMPT = {"/login", "/logout"}


def require_login(request: Request) -> None:
    """App-wide dependency that rejects requests without a logged-in session.

    It applies to every route, so new routes are protected unless they're added
    to LOGIN_EXEMPT.
    """
    if request.url.path in LOGIN_EXEMPT or request.session.get("logged_in"):
        return
    if "HX-Request" in request.headers:
        # htmx would follow a normal redirect inside the request and swap the
        # login page into the target. HX-Redirect navigates the whole page.
        raise HTTPException(
            status_code=401,
            detail="Your session has expired. Log in again.",
            headers={"HX-Redirect": "/login"},
        )
    if request.method == "GET":
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    raise HTTPException(status_code=401, detail="Your session has expired. Log in again.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    try:
        db.init_db(conn)
    finally:
        conn.close()
    yield


# The API docs are turned off. FastAPI serves them outside the dependency system,
# so require_login wouldn't protect them.
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
    """Send the board version a write produced, in an X-Board-Version header.

    The client uses it to avoid refetching its own change on the next poll (see
    adoptVersion in app.js).

    Only on a success: bump_version records the new version on the connection as
    a side effect, and a transaction that rolled back after bumping would leave
    it holding a version nobody else will ever see.
    """
    response = await call_next(request)
    conn = getattr(request.state, "db", None)
    if response.status_code < 400 and conn is not None and conn.board_version is not None:
        response.headers["X-Board-Version"] = str(conn.board_version)
    return response


# Added last so it runs first, before anything reads the session. The cookie is
# signed but not encrypted, and only holds the logged-in flag. Starlette re-sends
# it on every response, so the year counts from a device's last visit.
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
    """Collapse whitespace, including pasted newlines, to single spaces.

    Raises 400 if nothing is left. `what` names the field in the error message.
    """
    value = " ".join(value.split())
    if not value:
        raise HTTPException(status_code=400, detail=f"{what} can't be empty")
    return value


def id_list(ids: Iterable[int]) -> str:
    """Ids as JSON, for the `IN (SELECT value FROM json_each(?))` pattern.

    SQLite has no array parameter, and building an IN list by hand would mean
    interpolating the ids into the SQL. json_each keeps them parameters.
    """
    return json.dumps(list(ids))


# The attach_* pair fills in columns that need a second query. Both mutate the
# card dicts in place and return nothing, so a caller can't be in doubt about
# which copy is the up-to-date one.


def attach_labels(conn: sqlite3.Connection, cards: list[dict]) -> None:
    """Give each card dict `labels` and `people` lists, in creation order. One query."""
    by_id = {}
    for card in cards:
        card["labels"], card["people"] = [], []
        by_id[card["id"]] = card
    for row in conn.execute(
        """
        SELECT card_labels.card_id, labels.id, labels.name, labels.color, labels.kind
        FROM card_labels JOIN labels ON labels.id = card_labels.label_id
        WHERE card_labels.card_id IN (SELECT value FROM json_each(?))
        ORDER BY labels.id
        """,
        (id_list(by_id),),
    ):
        by_id[row["card_id"]]["people" if row["kind"] == "person" else "labels"].append(row)


def attach_progress(conn: sqlite3.Connection, cards: list[dict]) -> None:
    """Give each card dict `checklist_done` and `checklist_total`. One query.

    These are the counts shown on the card face. The modal loads the items
    themselves with load_checklist.
    """
    by_id = {}
    for card in cards:
        card["checklist_done"], card["checklist_total"] = 0, 0
        by_id[card["id"]] = card
    for row in conn.execute(
        """
        SELECT card_id, SUM(done) AS done, COUNT(*) AS total
        FROM checklist_items
        WHERE card_id IN (SELECT value FROM json_each(?))
        GROUP BY card_id
        """,
        (id_list(by_id),),
    ):
        by_id[row["card_id"]]["checklist_done"] = row["done"]
        by_id[row["card_id"]]["checklist_total"] = row["total"]


def load_checklist(conn: sqlite3.Connection, card_id: int) -> list[sqlite3.Row]:
    """A card's checklist items, in order."""
    return conn.execute(
        "SELECT * FROM checklist_items WHERE card_id = ? ORDER BY position, id", (card_id,)
    ).fetchall()


def load_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row:
    """One checklist item, plus its board_id for the version bump."""
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
    """One board. 404 if it doesn't exist."""
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
            """
            SELECT id, title FROM lists
            WHERE board_id = ? AND archived_at IS NULL
            ORDER BY position, id
            """,
            (board_id,),
        )
    ]
    cards = [
        dict(row)
        for row in conn.execute(
            """
            SELECT cards.id, cards.list_id, cards.title, cards.description != '' AS has_description
            FROM cards JOIN lists ON lists.id = cards.list_id
            WHERE lists.board_id = ? AND lists.archived_at IS NULL AND cards.archived_at IS NULL
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


# The same clock as the schema's created_at and updated_at defaults.
NOW = "strftime('%Y-%m-%d %H:%M:%f', 'now')"

ARCHIVABLE = ("boards", "lists", "cards")


def archive(conn: sqlite3.Connection, table: str, row_id: int) -> None:
    """Mark a row archived, now. Leaves an already-archived row alone, so its
    archived_at stays the moment it was archived rather than the last attempt."""
    assert table in ARCHIVABLE  # the table name is interpolated, so pin it to these
    conn.execute(
        f"UPDATE {table} SET archived_at = {NOW} WHERE id = ? AND archived_at IS NULL",
        (row_id,),
    )


def require_archived(row: sqlite3.Row, what: str) -> None:
    """Refuse to delete something that's still in use.

    Deleting for good is only offered from the archive, but a page open since
    before someone restored it would still show the button. Archiving is the one
    way in, so the check belongs here rather than only in the template.
    """
    if row["archived_at"] is None:
        raise HTTPException(status_code=409, detail=f"Archive this {what} before deleting it")


def load_archive(conn: sqlite3.Connection, board_id: int) -> dict[str, list]:
    """A board's archived lists and cards, newest first.

    Archiving a list doesn't archive its cards: they stay in it and come back
    with it, so they aren't listed separately. Each archived list shows how many
    cards a restore would bring back (`card_count`), and how many a delete would
    take (`total_count`, which includes any archived on their own beforehand and
    so listed below as well).
    """
    lists = conn.execute(
        """
        SELECT id, title, archived_at,
               (SELECT COUNT(*) FROM cards
                WHERE cards.list_id = lists.id AND cards.archived_at IS NULL) AS card_count,
               (SELECT COUNT(*) FROM cards WHERE cards.list_id = lists.id) AS total_count
        FROM lists
        WHERE board_id = ? AND archived_at IS NOT NULL
        ORDER BY archived_at DESC, id DESC
        """,
        (board_id,),
    ).fetchall()
    cards = conn.execute(
        """
        SELECT cards.id, cards.title, cards.archived_at, lists.title AS list_title
        FROM cards JOIN lists ON lists.id = cards.list_id
        WHERE lists.board_id = ? AND cards.archived_at IS NOT NULL
        ORDER BY cards.archived_at DESC, cards.id DESC
        """,
        (board_id,),
    ).fetchall()
    return {"lists": lists, "cards": cards}


# What _card_face.html draws. The description itself isn't among it — only
# whether there is one — so a face doesn't carry one around.
FACE_COLUMNS = """
    cards.id, cards.list_id, cards.title, cards.description != '' AS has_description,
    lists.board_id, lists.title AS list_title
"""


def load_faces(conn: sqlite3.Connection, card_ids: Iterable[int]) -> list[dict]:
    """Cards with what their face needs. Deleted ids are skipped.

    Renaming or deleting a label re-renders the face of every card carrying it,
    which is why this stays narrow.
    """
    rows = conn.execute(
        f"""
        SELECT {FACE_COLUMNS}
        FROM cards JOIN lists ON lists.id = cards.list_id
        WHERE cards.id IN (SELECT value FROM json_each(?))
        """,
        (id_list(card_ids),),
    )
    cards = [dict(row) for row in rows]
    attach_labels(conn, cards)
    attach_progress(conn, cards)
    return cards


def load_card(conn: sqlite3.Connection, card_id: int) -> dict:
    """One card in full: its face, plus the description and updated_at that the
    modal and the writes need. 404 if it's been deleted."""
    row = conn.execute(
        """
        SELECT cards.*, cards.description != '' AS has_description,
               lists.board_id, lists.title AS list_title
        FROM cards JOIN lists ON lists.id = cards.list_id
        WHERE cards.id = ?
        """,
        (card_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="This card was deleted")
    card = dict(row)
    attach_labels(conn, [card])
    attach_progress(conn, [card])
    return card


def board_labels(conn: sqlite3.Connection, board_id: int) -> dict[str, list]:
    """All of a board's labels, split by kind: {"label": [...], "person": [...]}."""
    options = {"label": [], "person": []}
    for row in conn.execute(
        "SELECT id, name, color, kind FROM labels WHERE board_id = ? ORDER BY id", (board_id,)
    ):
        options[row["kind"]].append(row)
    return options


def filter_options(options: dict[str, list]) -> list[dict]:
    """A board's labels, then its people, as JSON-ready dicts for the filter and
    the card composer (see _filter.html and the filter store in app.js)."""
    return [
        {
            "id": row["id"],
            "kind": row["kind"],
            "name": row["name"],
            "color": row["color"],
            "initials": initials(row["name"]),
        }
        for kind in ("label", "person")
        for row in options[kind]
    ]


templates.env.filters["filter_options"] = filter_options
templates.env.filters["markdown"] = markdown.render


def check_color(color: str) -> None:
    # Label colors go into style attributes, so this also keeps arbitrary CSS
    # out of the page.
    if color not in db.LABEL_COLORS.values():
        raise HTTPException(status_code=400, detail="Pick a color from the palette")


HEX_COLOR = re.compile(r"#[0-9a-f]{6}", re.IGNORECASE)


def clean_background(color: str) -> str:
    """Validate a board background: any #rrggbb color, returned lower-cased.

    The value is written into a <style> element, where HTML escaping doesn't
    help (`red; } body { display: none }` has nothing to escape), so only exact
    hex colors are accepted.
    """
    if not HEX_COLOR.fullmatch(color):
        raise HTTPException(status_code=400, detail="Pick a color")
    return color.lower()


# Uploaded backgrounds. Every upload is re-encoded, so the stored bytes are
# always a JPEG this app wrote.
UPLOAD_LIMIT = 12 * 1024 * 1024  # the most that's read from an upload
BACKGROUND_SIZE = (1920, 1920)  # a board background is never shown larger
BACKGROUND_TYPE = "image/jpeg"


def clean_background_image(data: bytes) -> bytes:
    """Turn an uploaded file into a JPEG to use as a board background.

    The upload is decoded and re-encoded, so only an image this app wrote is
    ever served back. An HTML file named .jpg fails to decode and is rejected.
    Re-encoding drops EXIF, including where a photo was taken. Shrinking keeps
    a phone photo from being sent at full size to every device that opens the
    board.
    """
    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))  # apply the rotation flag
        image.thumbnail(BACKGROUND_SIZE)  # never enlarges, and keeps the aspect ratio
        # JPEG has no transparency. Converting straight to RGB would put
        # transparent areas on black, so they're composited onto white below.
        image = image.convert("RGBA")
    except Exception:  # Pillow raises several different errors for a file it can't read
        raise HTTPException(status_code=400, detail="That doesn't look like an image")

    flat = Image.new("RGB", image.size, "white")
    flat.paste(image, mask=image.getchannel("A"))
    out = io.BytesIO()
    flat.save(out, "JPEG", quality=82, optimize=True, progressive=True)
    return out.getvalue()


def relative_luminance(color: str) -> float:
    """WCAG relative luminance of a #rrggbb color: 0 for black, 1 for white."""

    def linear(channel: int) -> float:
        c = channel / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)


def needs_dark_ink(background: str) -> bool:
    """Whether text on this background should be dark instead of white.

    Text stays white unless its contrast drops below 3:1, the WCAG minimum for
    large text and controls. Choosing whichever color has more contrast would
    switch several presets to dark text over very small differences.

    A photo has no single brightness to measure, so uploaded backgrounds always
    get white text. The header and the lists panel are shaded over the photo,
    and everything else on the board has an opaque background of its own.
    """
    if not HEX_COLOR.fullmatch(background):
        return False
    contrast_with_white = 1.05 / (relative_luminance(background) + 0.05)
    return contrast_with_white < 3


def image_token(background: str) -> str | None:
    """The token in an 'image:<token>' background, or None for a color."""
    prefix, _, token = background.partition(":")
    return token if prefix == "image" else None


def background_css(board: sqlite3.Row) -> str:
    """The CSS `background` value for a board: its color, or its uploaded image.

    This goes into a <style> element, like clean_background's color, so it must
    not be attacker-controlled. The board id is an integer and the token is hex
    from secrets, so nothing else can reach the URL. The url() is unquoted
    because Jinja escapes quotes inside a style, and CSS allows it.
    """
    token = image_token(board["background"])
    if token is None:
        return board["background"]
    return f"url(/boards/{board['id']}/background/{token}) center / cover no-repeat"


templates.env.filters["needs_dark_ink"] = needs_dark_ink
templates.env.filters["background_css"] = background_css
templates.env.filters["image_token"] = image_token


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

    compare_digest takes the same time no matter how much of a guess matches.
    """
    if not secrets.compare_digest(password.encode(), BOARD_PASSWORD.encode()):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Wrong password."}, status_code=401
        )
    request.session["logged_in"] = True
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def index_tiles(conn: sqlite3.Connection) -> dict[str, list]:
    """The index's boards, and its archived ones for the section below them."""
    boards = conn.execute(
        """
        SELECT id, title, background FROM boards
        WHERE archived_at IS NULL ORDER BY created_at, id
        """
    ).fetchall()
    archived = conn.execute(
        """
        SELECT id, title, background FROM boards
        WHERE archived_at IS NOT NULL ORDER BY archived_at DESC, id DESC
        """
    ).fetchall()
    return {"boards": boards, "archived": archived}


@app.get("/", response_class=HTMLResponse)
def board_index(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    return templates.TemplateResponse(request, "index.html", index_tiles(conn))


@app.post("/boards")
def create_board(
    title: Annotated[str, Form()], conn: sqlite3.Connection = Depends(db.get_db)
):
    """Create an empty board and redirect to it.

    Uses HX-Redirect instead of a 303 so errors show up as a flash message, the
    same as everywhere else.
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
            "options": board_labels(conn, board_id),
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
    """Rename a board or set its background. Send whichever one changed.

    A rename returns the header, so the title field's value attribute (which
    Escape reverts to) matches the saved title. A background change returns only
    the settings menu, so the menu stays open.
    """
    if title is None and background is None:
        # Writing the row back unchanged would still bump the version, and send
        # every other client to refetch the whole board for nothing.
        raise HTTPException(status_code=400, detail="That save was empty")
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
        if background is not None:
            # One background per board, so picking a color drops the photo.
            conn.execute("DELETE FROM board_images WHERE board_id = ?", (board_id,))
        db.bump_version(conn, board_id)

    board = load_board(conn, board_id)
    if background is not None:
        return templates.TemplateResponse(request, "_board_menu_saved.html", {"board": board})
    return templates.TemplateResponse(
        request,
        "_board_header.html",
        {"board": board, "options": board_labels(conn, board_id)},
    )


def archive_response(request: Request, conn: sqlite3.Connection, board_id: int) -> Response:
    """The archive dialog's contents, plus the board's lists out-of-band.

    The board's lists are sent with the response because a client's own write
    never comes back to it through a poll (see adoptVersion in app.js).
    """
    return templates.TemplateResponse(
        request,
        "_archive_refresh.html",
        {
            "board": load_board(conn, board_id),
            "archive": load_archive(conn, board_id),
            "lists": load_lists(conn, board_id),
        },
    )


@app.get("/boards/{board_id}/archive", response_class=HTMLResponse)
def board_archive(
    board_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)
):
    """The archive dialog, loaded when it's opened rather than with the board."""
    return templates.TemplateResponse(
        request,
        "_archive_modal.html",
        {"board": load_board(conn, board_id), "archive": load_archive(conn, board_id)},
    )


@app.post("/boards/{board_id}/archive")
def archive_board(board_id: int, conn: sqlite3.Connection = Depends(db.get_db)):
    """Take a board off the index, keeping everything on it."""
    with conn:
        load_board(conn, board_id)
        archive(conn, "boards", board_id)
        db.bump_version(conn, board_id)
    return Response(status_code=204, headers={"HX-Redirect": "/"})


@app.post("/boards/{board_id}/restore", response_class=HTMLResponse)
def restore_board(
    board_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)
):
    """Put an archived board back on the index. Returns the index's tiles."""
    with conn:
        load_board(conn, board_id)
        conn.execute("UPDATE boards SET archived_at = NULL WHERE id = ?", (board_id,))
        db.bump_version(conn, board_id)
    return templates.TemplateResponse(request, "_board_tiles.html", index_tiles(conn))


@app.post("/boards/{board_id}/background", response_class=HTMLResponse)
def upload_background(
    request: Request,
    board_id: int,
    image: Annotated[UploadFile, File()],
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Set a board's background to an uploaded photo.

    Returns the settings menu, like picking a color does, so the menu stays
    open. The board's background becomes 'image:<token>'. The token is also in
    the image's URL, so a replacement photo gets a URL nothing has cached.
    """
    data = image.file.read(UPLOAD_LIMIT + 1)
    if len(data) > UPLOAD_LIMIT:
        raise HTTPException(status_code=400, detail="That image is too big (12MB at most)")
    jpeg = clean_background_image(data)
    token = secrets.token_hex(8)

    with conn:
        load_board(conn, board_id)
        conn.execute(
            """
            INSERT INTO board_images (board_id, token, content_type, data) VALUES (?, ?, ?, ?)
            ON CONFLICT (board_id) DO UPDATE
                SET token = excluded.token, content_type = excluded.content_type,
                    data = excluded.data
            """,
            (board_id, token, BACKGROUND_TYPE, jpeg),
        )
        conn.execute(
            "UPDATE boards SET background = ? WHERE id = ?", (f"image:{token}", board_id)
        )
        db.bump_version(conn, board_id)

    return templates.TemplateResponse(
        request, "_board_menu_saved.html", {"board": load_board(conn, board_id)}
    )


@app.get("/boards/{board_id}/background/{token}")
def board_background(
    board_id: int, token: str, conn: sqlite3.Connection = Depends(db.get_db)
):
    """A board's uploaded background.

    A new upload gets a new token and a new URL, so this response can be cached
    indefinitely. This is a normal route, so require_login applies to it; files
    under /static are a mount, and mounts skip app-wide dependencies.
    """
    row = conn.execute(
        "SELECT content_type, data FROM board_images WHERE board_id = ? AND token = ?",
        (board_id, token),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="This image is gone")
    return Response(
        row["data"],
        media_type=row["content_type"],
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            # Don't let a browser guess the type from the bytes.
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.delete("/boards/{board_id}", response_class=HTMLResponse)
def delete_board(
    board_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)
):
    """Delete an archived board and everything on it. Returns the index's tiles.

    There's no version to bump once the row is gone. Other clients' polls get a
    286 instead.
    """
    with conn:
        require_archived(load_board(conn, board_id), "board")
        conn.execute("DELETE FROM boards WHERE id = ?", (board_id,))
    return templates.TemplateResponse(request, "_board_tiles.html", index_tiles(conn))


@app.get("/boards/{board_id}/poll", response_class=HTMLResponse)
def poll_board(
    board_id: int, v: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)
):
    """204 if version `v` is current, otherwise the re-rendered lists container.

    The version is read before the lists. If a write lands in between, the
    content is newer than the version sent with it, so the next poll fetches
    again and no change is missed.
    """
    board = conn.execute("SELECT * FROM boards WHERE id = ?", (board_id,)).fetchone()
    if board is None or board["archived_at"] is not None:
        # 286 tells htmx to stop polling; the message replaces the lists. An
        # archived board still renders, so this is how someone else looking at
        # it finds out that it has left the index.
        gone = "deleted" if board is None else "archived"
        return HTMLResponse(
            f'<div id="lists-container" class="lists board-gone">This board was {gone}.</div>',
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
            "options": board_labels(conn, board_id),
        },
    )


@app.post("/lists", response_class=HTMLResponse)
def create_list(
    request: Request,
    board_id: Annotated[int, Form()],
    title: Annotated[str, Form()],
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Add a list to the end of a board. Returns the new list's HTML."""
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
    """Rename a list. Returns the re-rendered list header."""
    title = clean_text(title, "List title")
    with conn:
        lst = load_list(conn, list_id)
        conn.execute("UPDATE lists SET title = ? WHERE id = ?", (title, list_id))
        db.bump_version(conn, lst["board_id"])
    return templates.TemplateResponse(
        request, "_list_header.html", {"list": load_list(conn, list_id)}
    )


@app.delete("/lists/{list_id}", response_class=HTMLResponse)
def delete_list(
    list_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)
):
    """Delete an archived list and its cards for good, from the archive dialog."""
    with conn:
        lst = load_list(conn, list_id)
        require_archived(lst, "list")
        conn.execute("DELETE FROM lists WHERE id = ?", (list_id,))
        db.bump_version(conn, lst["board_id"])
    return archive_response(request, conn, lst["board_id"])


@app.post("/lists/{list_id}/archive", response_class=HTMLResponse)
def archive_list(list_id: int, conn: sqlite3.Connection = Depends(db.get_db)):
    """Take a list off the board.

    Its cards aren't archived with it. The board doesn't render an archived
    list, so they go off the board with it and come back when it does.
    """
    with conn:
        lst = load_list(conn, list_id)
        archive(conn, "lists", list_id)
        db.bump_version(conn, lst["board_id"])
    return HTMLResponse("")


@app.post("/lists/{list_id}/restore", response_class=HTMLResponse)
def restore_list(
    list_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)
):
    """Put an archived list back on the board, with the cards it still holds."""
    with conn:
        lst = load_list(conn, list_id)
        conn.execute("UPDATE lists SET archived_at = NULL WHERE id = ?", (list_id,))
        db.bump_version(conn, lst["board_id"])
    return archive_response(request, conn, lst["board_id"])


@app.post("/lists/{list_id}/archive-cards", response_class=HTMLResponse)
def archive_list_cards(
    list_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)
):
    """Archive every card in a list, keeping the list itself.

    Returns the list's empty card container, so the board shows the result
    without waiting for a poll.
    """
    with conn:
        lst = load_list(conn, list_id)
        conn.execute(
            f"UPDATE cards SET archived_at = {NOW} WHERE list_id = ? AND archived_at IS NULL",
            (list_id,),
        )
        db.bump_version(conn, lst["board_id"])
    return templates.TemplateResponse(
        request, "_cards.html", {"list": dict(lst, cards=[])}
    )


@app.post("/cards", response_class=HTMLResponse)
def create_card(
    request: Request,
    list_id: Annotated[int, Form()],
    title: Annotated[str, Form()],
    label_ids: Annotated[list[int] | None, Form()] = None,
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Add a card to the bottom of a list. Returns the new card's HTML.

    `label_ids` are labels and people to assign right away: the ones the board
    filter is showing (see the composer in _list.html). Ids that aren't on the
    list's board, for example deleted since the page loaded, are skipped.
    """
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
        conn.execute(
            """
            INSERT OR IGNORE INTO card_labels (card_id, label_id)
            SELECT ?, id FROM labels
            WHERE board_id = ? AND id IN (SELECT value FROM json_each(?))
            """,
            (card_id, lst["board_id"], id_list(label_ids or [])),
        )
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
    """Renumber a parent's children 0, 1, 2…, starting with `ordered_ids` in order.

    `ordered_ids` are moved under the parent if needed; the caller must check
    they're on the same board. Children not in `ordered_ids` (for example, added
    by someone else since the page loaded) go after them in their current order.
    `exclude` holds ids the same request is moving to another parent.

    Every drag writes positions through here. Moves don't change
    cards.updated_at, which is the conflict token for description edits.
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
    from_card_ids: Annotated[list[int] | None, Form()] = None,
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Save card order after a drag.

    Takes the full card order of the list the card was dropped in and, for a
    move between lists, the full order of the list it left (possibly empty).
    """
    from_card_ids = from_card_ids or []
    orders = {list_id: card_ids}
    if from_list_id is not None and from_list_id != list_id:
        orders[from_list_id] = from_card_ids

    with conn:
        lists = conn.execute(
            "SELECT id, board_id FROM lists WHERE id IN (SELECT value FROM json_each(?))",
            (id_list(orders),),
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
                (board_id, id_list(card_ids + from_card_ids)),
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
                (board_id, id_list(list_ids)),
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


@app.get("/cards/{card_id}/status")
def card_status(card_id: int, conn: sqlite3.Connection = Depends(db.get_db)):
    """Whether a card is still on its board, only archived, or gone for good.

    The focus timer asks this when the card it's tracking isn't on the page.
    Archiving takes a card off the board, and so does archiving its list or its
    board, all of which look exactly like a deletion from the client's side. A
    card that's only archived stays in the dock, because restoring it brings it
    back (see syncFocusWithPage in timer.js).
    """
    row = conn.execute(
        """
        SELECT (cards.archived_at IS NOT NULL
                OR lists.archived_at IS NOT NULL
                OR boards.archived_at IS NOT NULL) AS archived
        FROM cards
        JOIN lists ON lists.id = cards.list_id
        JOIN boards ON boards.id = lists.board_id
        WHERE cards.id = ?
        """,
        (card_id,),
    ).fetchone()
    if row is None:
        return {"state": "gone"}
    return {"state": "archived" if row["archived"] else "board"}


@app.patch("/cards/{card_id}", response_class=HTMLResponse)
def update_card(
    card_id: int,
    request: Request,
    updated_at: Annotated[str, Form()],
    title: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Save the modal's title, its description, or both.

    The two are separate forms, so a request normally carries one of them: the
    title saves itself when it loses focus, while the description is saved by
    hand, and neither should drag the other's half-finished text along with it.
    A field that isn't sent is left as it is.

    `updated_at` is the value the modal was loaded with, shared by both forms
    (see #card-updated-at). If the card has been saved since, this save is
    rejected with a 409 carrying the stored description and the current token,
    so the description editor can offer to overwrite that version, take it, or
    merge the two (see descriptionEditor in app.js).
    """
    # Column names come from here, not from the request.
    changes = {}
    if title is not None:
        changes["title"] = clean_text(title, "Card title")
    if description is not None:
        changes["description"] = "\n".join(description.splitlines()).strip()  # browsers send \r\n
    if not changes:
        raise HTTPException(status_code=400, detail="That save was empty")

    with conn:
        card = load_card(conn, card_id)
        stale = card["updated_at"] != updated_at
        if not stale:
            assignments = ", ".join(f"{column} = ?" for column in changes)
            conn.execute(
                f"UPDATE cards SET {assignments}, updated_at = {NOW} WHERE id = ?",
                (*changes.values(), card_id),
            )
            db.bump_version(conn, card["board_id"])
    if stale:
        # Not an HTTPException: the editor needs the version it's up against,
        # not just a message. Returned after the transaction so the rejected
        # save leaves nothing behind.
        return JSONResponse(
            status_code=409,
            content={
                "detail": "Someone else changed this card while you were editing it.",
                "description": card["description"],
                "updated_at": card["updated_at"],
            },
        )
    return templates.TemplateResponse(
        request, "_card_saved.html", {"card": load_card(conn, card_id)}
    )


@app.delete("/cards/{card_id}", response_class=HTMLResponse)
def delete_card(
    card_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)
):
    """Delete an archived card for good. Returns the archive dialog's contents."""
    with conn:
        card = load_card(conn, card_id)
        require_archived(card, "card")
        conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
        db.bump_version(conn, card["board_id"])
    return archive_response(request, conn, card["board_id"])


@app.post("/cards/{card_id}/archive", response_class=HTMLResponse)
def archive_card(card_id: int, conn: sqlite3.Connection = Depends(db.get_db)):
    """Take a card off the board, keeping everything on it.

    Returns an empty 200 because htmx skips swapping on a 204, and the swap is
    what removes the card from the page. The gap left in the list's positions
    doesn't affect the order, and the next drag in that list renumbers it.
    """
    with conn:
        card = load_card(conn, card_id)
        archive(conn, "cards", card_id)
        db.bump_version(conn, card["board_id"])
    return HTMLResponse("")


@app.post("/cards/{card_id}/restore", response_class=HTMLResponse)
def restore_card(
    card_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)
):
    """Put an archived card back on its list.

    Restores the list too if that's archived, so the card comes back somewhere
    visible rather than into a column nobody can see.
    """
    with conn:
        card = load_card(conn, card_id)
        conn.execute("UPDATE cards SET archived_at = NULL WHERE id = ?", (card_id,))
        conn.execute(
            "UPDATE lists SET archived_at = NULL WHERE id = ?", (card["list_id"],)
        )
        db.bump_version(conn, card["board_id"])
    return archive_response(request, conn, card["board_id"])


def label_section(
    request: Request,
    conn: sqlite3.Connection,
    card_id: int,
    kind: str,
    face_ids: Iterable[int],
    *,
    managing: bool = False,
    options_changed: bool = False,
):
    """Re-render the modal's labels or people section after a change.

    Also swaps out-of-band the faces of `face_ids` (every card showing what
    changed) and, if a label or person was added, renamed, recolored or
    deleted, the header's filter, which lists them.
    `managing` keeps the section in its edit view.
    """
    card = load_card(conn, card_id)
    options = board_labels(conn, card["board_id"])
    return templates.TemplateResponse(
        request,
        "_label_section.html",
        {
            "card": card,
            "kind": kind,
            "options": options[kind],
            "board_options": options,
            "managing": managing,
            "faces": load_faces(conn, face_ids),
            "refresh_filter": options_changed,
        },
    )


@app.post("/cards/{card_id}/labels", response_class=HTMLResponse)
def add_card_label(
    card_id: int,
    request: Request,
    label_id: Annotated[int, Form()],
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Assign a label or person to a card.

    This is idempotent instead of a toggle, so a stale modal can't undo someone
    else's change.
    """
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

    Labels are created from the card modal, so this takes a card and uses its
    board. Labels pick a palette color; people get the next one automatically.
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
    return label_section(request, conn, card_id, kind, [card_id], options_changed=True)


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
        options_changed=True,
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
        request, conn, card_id, label["kind"], affected, managing=True, options_changed=True
    )


def checklist_body(request: Request, conn: sqlite3.Connection, card_id: int):
    """The response to every checklist write.

    Re-renders the modal's checklist, plus the card face out-of-band, since it
    shows the same done/total count.
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
    """Tick an item or edit its text. Send whichever one changed.

    `done` is the value to set. A toggle would let a stale modal undo someone
    else's change; the label endpoints avoid toggles for the same reason.
    """
    if done is None and text is None:
        raise HTTPException(status_code=400, detail="That save was empty")
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
    """Delete a checklist item. As with cards, the gap left in positions is fine."""
    with conn:
        item = load_item(conn, item_id)
        conn.execute("DELETE FROM checklist_items WHERE id = ?", (item_id,))
        db.bump_version(conn, item["board_id"])
    return checklist_body(request, conn, item["card_id"])
