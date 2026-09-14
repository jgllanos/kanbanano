import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
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
