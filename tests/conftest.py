"""Fixtures and assertion helpers shared by the tests.

Each test gets its own empty database. The app is pointed at it by patching
`db.DB_PATH` instead of overriding the `get_db` dependency, so requests use the
real per-request connections, including the X-Board-Version middleware.

`client` is logged in; `anon` isn't, for testing what's kept out.
"""

import os
import sqlite3
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

# The app reads these at import, and refuses to start without them.
PASSWORD = "correct horse"
os.environ["BOARD_PASSWORD"] = PASSWORD
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ.pop("ALLOW_HTTP", None)  # test the Secure cookie production uses

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """An empty database, and a connection to it for setting up and checking."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "board.db")
    connection = db.connect()
    db.init_db(connection)
    yield connection
    connection.close()


def make_client() -> TestClient:
    # Not used as a context manager, which would run the app's lifespan and create
    # the schema in the real database. HTTPS because the session cookie is Secure,
    # and the client won't send it over HTTP.
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def anon(conn) -> TestClient:
    return make_client()


@pytest.fixture
def client(conn) -> TestClient:
    logged_in = make_client()
    response = logged_in.post("/login", data={"password": PASSWORD})
    assert response.status_code == 200, "logging in failed"  # 200: after following the redirect
    return logged_in


# ---- Sample data ----------------------------------------------------------


@dataclass
class Board:
    """A board with two lists: "To do" holding three cards, and an empty "Done"."""

    id: int
    todo: int
    done: int
    cards: list[int] = field(default_factory=list)


@pytest.fixture
def board(conn) -> Board:
    with conn:
        board_id = conn.execute("INSERT INTO boards (title) VALUES ('Home')").lastrowid
        todo, done = (
            conn.execute(
                "INSERT INTO lists (board_id, title, position) VALUES (?, ?, ?)",
                (board_id, title, position),
            ).lastrowid
            for position, title in enumerate(["To do", "Done"])
        )
        cards = [
            conn.execute(
                "INSERT INTO cards (list_id, title, position) VALUES (?, ?, ?)",
                (todo, title, position),
            ).lastrowid
            for position, title in enumerate(["Tap", "Bins", "Shelf"])
        ]
    return Board(id=board_id, todo=todo, done=done, cards=cards)


# ---- Request helpers ------------------------------------------------------


def delete_for_good(client: TestClient, kind: str, row_id: int, **params):
    """Delete a board, list or card permanently, the way the UI does.

    Deleting is only offered from the archive, and the endpoints refuse anything
    still in use, so getting rid of something takes both steps. Tests that only
    need the thing gone use this; the ones about archiving spell it out.
    """
    client.post(f"/{kind}/{row_id}/archive")
    return client.request("DELETE", f"/{kind}/{row_id}", params=params)


# ---- Assertion helpers ----------------------------------------------------


def version(conn: sqlite3.Connection, board_id: int) -> int:
    (current,) = conn.execute("SELECT version FROM boards WHERE id = ?", (board_id,)).fetchone()
    return current


def ordering(conn: sqlite3.Connection, table: str, parent_column: str, parent_id: int):
    """Children in the order the app reads them, as (id, position) pairs."""
    return [
        (row["id"], row["position"])
        for row in conn.execute(
            f"SELECT id, position FROM {table} WHERE {parent_column} = ? ORDER BY position, id",
            (parent_id,),
        )
    ]


def card_order(conn: sqlite3.Connection, list_id: int) -> list[int]:
    """A list's card ids in order. Positions are only guaranteed contiguous after
    a reorder (see test_reorder)."""
    return [card_id for card_id, _ in ordering(conn, "cards", "list_id", list_id)]


def list_order(conn: sqlite3.Connection, board_id: int) -> list[int]:
    """A board's list ids in order, as card_order."""
    return [list_id for list_id, _ in ordering(conn, "lists", "board_id", board_id)]
