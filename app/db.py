import os
import sqlite3
from pathlib import Path

from fastapi import Request

DB_PATH = Path(os.environ.get("DB_PATH", "board.db"))
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# The only colors labels and people can have. Light enough for dark text.
# Colors end up in style attributes, so never store anything not in this list.
LABEL_COLORS = {
    "green": "#4bce97",
    "yellow": "#f5cd47",
    "orange": "#fea362",
    "red": "#f87168",
    "purple": "#9f8fef",
    "blue": "#579dff",
    "sky": "#6cc3e0",
    "lime": "#94c748",
    "pink": "#e774bb",
    "gray": "#8590a2",
}


class Connection(sqlite3.Connection):
    # The board version after this connection's last bump_version, if any.
    # main.py reports it to the client in the X-Board-Version header.
    board_version: int | None = None


def connect(path: Path = DB_PATH) -> Connection:
    # check_same_thread=False: FastAPI may run a sync dependency and the endpoint
    # using it on different threadpool threads. Each request still gets its own
    # connection, used by one thread at a time, so this is safe.
    conn = sqlite3.connect(path, check_same_thread=False, factory=Connection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())


def get_db(request: Request):
    """FastAPI dependency: one connection per request.

    Also stored on request.state, where the X-Board-Version middleware finds it.
    """
    conn = connect()
    request.state.db = conn
    try:
        yield conn
    finally:
        conn.close()


def bump_version(conn: Connection, board_id: int) -> None:
    """Mark a board as changed so pollers refetch it.

    Every write path that touches a board or anything on it (lists, cards,
    labels, checklist items) must call this inside the same transaction.
    """
    conn.execute("UPDATE boards SET version = version + 1 WHERE id = ?", (board_id,))
    (conn.board_version,) = conn.execute(
        "SELECT version FROM boards WHERE id = ?", (board_id,)
    ).fetchone()
