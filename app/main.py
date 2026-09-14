import json
import sqlite3
from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db

BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    try:
        db.init_db(conn)
    finally:
        conn.close()
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def load_lists(conn: sqlite3.Connection, board_id: int) -> list[dict]:
    """A board's lists in order, each with its cards in order. Two queries total."""
    lists = [
        dict(row)
        for row in conn.execute(
            "SELECT id, title FROM lists WHERE board_id = ? ORDER BY position, id",
            (board_id,),
        )
    ]
    cards_by_list = {lst["id"]: [] for lst in lists}
    for card in conn.execute(
        """
        SELECT cards.id, cards.list_id, cards.title
        FROM cards JOIN lists ON lists.id = cards.list_id
        WHERE lists.board_id = ?
        ORDER BY cards.position, cards.id
        """,
        (board_id,),
    ):
        cards_by_list[card["list_id"]].append(card)
    for lst in lists:
        lst["cards"] = cards_by_list[lst["id"]]
    return lists


@app.get("/", response_class=HTMLResponse)
def board_index(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    boards = conn.execute(
        "SELECT id, title, background FROM boards ORDER BY created_at, id"
    ).fetchall()
    return templates.TemplateResponse(request, "index.html", {"boards": boards})


@app.get("/boards/{board_id}", response_class=HTMLResponse)
def board_view(
    board_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)
):
    board = conn.execute("SELECT * FROM boards WHERE id = ?", (board_id,)).fetchone()
    if board is None:
        raise HTTPException(status_code=404, detail="Board not found")
    return templates.TemplateResponse(
        request, "board.html", {"board": board, "lists": load_lists(conn, board_id)}
    )


@app.post("/cards", response_class=HTMLResponse)
def create_card(
    request: Request,
    list_id: Annotated[int, Form()],
    title: Annotated[str, Form()],
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Append a card to the bottom of a list. Returns just the new card's HTML."""
    title = " ".join(title.split())  # titles are one line; collapse pasted newlines
    if not title:
        raise HTTPException(status_code=400, detail="Card title can't be empty")

    with conn:
        lst = conn.execute("SELECT board_id FROM lists WHERE id = ?", (list_id,)).fetchone()
        if lst is None:
            raise HTTPException(status_code=404, detail="This list was deleted")
        card_id = conn.execute(
            """
            INSERT INTO cards (list_id, title, position)
            VALUES (?, ?, (SELECT COALESCE(MAX(position) + 1, 0) FROM cards WHERE list_id = ?))
            """,
            (list_id, title, list_id),
        ).lastrowid
        db.bump_version(conn, lst["board_id"])

    card = conn.execute("SELECT id, title FROM cards WHERE id = ?", (card_id,)).fetchone()
    return templates.TemplateResponse(request, "_card.html", {"card": card})


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
        if conn.execute("SELECT 1 FROM boards WHERE id = ?", (board_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="This board was deleted")
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
