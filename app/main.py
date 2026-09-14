import sqlite3
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
