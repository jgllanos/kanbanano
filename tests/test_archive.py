"""Archiving cards, lists and boards, and the archive dialog.

Archiving replaces deleting on the board, so these check two things: an
archived row stays out of what the board renders, and it comes back whole.
"""

import sqlite3

import pytest

from app import db
from conftest import version


def archived(conn: sqlite3.Connection, table: str, row_id: int) -> bool:
    (stamp,) = conn.execute(
        f"SELECT archived_at FROM {table} WHERE id = ?", (row_id,)
    ).fetchone()
    return stamp is not None


def board_cards(client, board_id: int) -> str:
    return client.get(f"/boards/{board_id}").text


# ---- Cards ---------------------------------------------------------------


def test_archiving_a_card_takes_it_off_the_board(client, conn, board):
    card = board.cards[0]

    response = client.post(f"/cards/{card}/archive")

    assert response.status_code == 200
    assert archived(conn, "cards", card)
    assert version(conn, board.id) == 1
    assert "Tap" not in board_cards(client, board.id)
    assert "Bins" in board_cards(client, board.id)  # its neighbours stay


def test_an_archived_card_keeps_everything(client, conn, board):
    """Archiving is one UPDATE, so nothing hanging off the card is touched."""
    card = board.cards[0]
    with conn:
        label = conn.execute(
            "INSERT INTO labels (board_id, name, color) VALUES (?, 'House', '#4bce97')",
            (board.id,),
        ).lastrowid
        conn.execute("UPDATE cards SET description = 'Drips' WHERE id = ?", (card,))
    client.post(f"/cards/{card}/labels", data={"label_id": label})
    client.post(f"/cards/{card}/checklist", data={"text": "Washer"})

    client.post(f"/cards/{card}/archive")
    client.post(f"/cards/{card}/restore")

    stored = conn.execute("SELECT * FROM cards WHERE id = ?", (card,)).fetchone()
    assert (stored["title"], stored["description"]) == ("Tap", "Drips")
    assert conn.execute("SELECT COUNT(*) FROM card_labels").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM checklist_items").fetchone()[0] == 1


def test_restoring_a_card_puts_it_back_in_its_list(client, conn, board):
    card = board.cards[0]
    client.post(f"/cards/{card}/archive")

    response = client.post(f"/cards/{card}/restore")

    assert not archived(conn, "cards", card)
    assert "Tap" in board_cards(client, board.id)
    # The dialog's contents, plus the board out-of-band so it catches up.
    assert 'id="archive-body"' in response.text
    assert 'id="lists-container"' in response.text and "hx-swap-oob" in response.text


def test_restoring_a_card_brings_back_its_archived_list(client, conn, board):
    """Otherwise the card comes back into a column nobody can see."""
    card = board.cards[0]
    client.post(f"/cards/{card}/archive")
    client.post(f"/lists/{board.todo}/archive")

    client.post(f"/cards/{card}/restore")

    assert not archived(conn, "lists", board.todo)
    assert "Tap" in board_cards(client, board.id)


def test_deleting_an_archived_card_for_good(client, conn, board):
    card = board.cards[0]
    client.post(f"/cards/{card}/archive")

    client.request("DELETE", f"/cards/{card}")

    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 2
    assert version(conn, board.id) == 2


def test_archiving_a_deleted_card_404s(client, conn, board):
    card = board.cards[0]
    client.request("DELETE", f"/cards/{card}")

    assert client.post(f"/cards/{card}/archive").status_code == 404
    assert client.post(f"/cards/{card}/restore").status_code == 404


# ---- Lists ---------------------------------------------------------------


def test_archiving_a_list_takes_its_cards_off_the_board_too(client, conn, board):
    """The cards aren't archived themselves; the board just doesn't render them."""
    client.post(f"/lists/{board.todo}/archive")

    page = board_cards(client, board.id)
    assert "To do" not in page and "Tap" not in page
    assert "Done" in page  # the board's other list
    assert archived(conn, "lists", board.todo)
    assert conn.execute("SELECT COUNT(*) FROM cards WHERE archived_at IS NOT NULL").fetchone()[0] == 0


def test_restoring_a_list_brings_its_cards_back_with_it(client, conn, board):
    client.post(f"/lists/{board.todo}/archive")

    client.post(f"/lists/{board.todo}/restore")

    page = board_cards(client, board.id)
    for title in ["To do", "Tap", "Bins", "Shelf"]:
        assert title in page


def test_archiving_every_card_in_a_list_keeps_the_list(client, conn, board):
    response = client.post(f"/lists/{board.todo}/archive-cards")

    page = board_cards(client, board.id)
    assert "To do" in page
    for title in ["Tap", "Bins", "Shelf"]:
        assert title not in page
    assert not archived(conn, "lists", board.todo)
    assert version(conn, board.id) == 1
    # The list's empty card container, so the board shows it without a poll.
    assert f'id="cards-{board.todo}"' in response.text


def test_archiving_every_card_leaves_already_archived_ones_alone(client, conn, board):
    """Their archived_at is when they were archived, not when the list was emptied."""
    first = board.cards[0]
    client.post(f"/cards/{first}/archive")
    (stamp,) = conn.execute("SELECT archived_at FROM cards WHERE id = ?", (first,)).fetchone()

    client.post(f"/lists/{board.todo}/archive-cards")

    assert conn.execute(
        "SELECT archived_at FROM cards WHERE id = ?", (first,)
    ).fetchone()[0] == stamp


def test_deleting_an_archived_list_takes_its_cards(client, conn, board):
    client.post(f"/lists/{board.todo}/archive")

    client.request("DELETE", f"/lists/{board.todo}")

    assert conn.execute("SELECT COUNT(*) FROM lists").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0


# ---- The archive dialog --------------------------------------------------


def test_the_archive_lists_what_was_archived(client, board):
    client.post(f"/cards/{board.cards[0]}/archive")
    client.post(f"/lists/{board.done}/archive")

    page = client.get(f"/boards/{board.id}/archive").text

    assert "Tap" in page and "from To do" in page  # the card, and where it came from
    assert "Done" in page and "0 cards" in page  # the list, and what it holds
    assert "Bins" not in page  # still on the board


def test_an_empty_archive_says_so(client, board):
    page = client.get(f"/boards/{board.id}/archive").text

    assert "Nothing archived" in page


def test_cards_in_an_archived_list_are_not_listed_separately(client, board):
    """They come back with the list, so they aren't restored on their own."""
    client.post(f"/lists/{board.todo}/archive")

    page = client.get(f"/boards/{board.id}/archive").text

    assert "To do" in page and "3 cards" in page
    assert "from To do" not in page


# ---- Boards --------------------------------------------------------------


def test_archiving_a_board_takes_it_off_the_index(client, conn, board):
    response = client.post(f"/boards/{board.id}/archive")

    assert response.headers["hx-redirect"] == "/"
    assert archived(conn, "boards", board.id)
    index = client.get("/").text
    assert "Archived" in index and "Home" in index  # in its own section
    assert f'href="/boards/{board.id}"' not in index  # and not as a link


def test_an_archived_board_still_opens_but_stops_polling(client, board):
    """Someone already looking at it finds out from their next poll."""
    client.post(f"/boards/{board.id}/archive")

    assert client.get(f"/boards/{board.id}").status_code == 200
    poll = client.get(f"/boards/{board.id}/poll", params={"v": 0})
    assert poll.status_code == 286
    assert "This board was archived" in poll.text


def test_restoring_a_board_puts_it_back_on_the_index(client, conn, board):
    client.post(f"/boards/{board.id}/archive")

    response = client.post(f"/boards/{board.id}/restore")

    assert not archived(conn, "boards", board.id)
    assert f'href="/boards/{board.id}"' in response.text
    assert "Archived" not in response.text  # the section is gone with it


def test_an_archived_board_keeps_its_lists_and_cards(client, conn, board):
    client.post(f"/boards/{board.id}/archive")
    client.post(f"/boards/{board.id}/restore")

    page = board_cards(client, board.id)
    for title in ["To do", "Tap", "Bins", "Shelf"]:
        assert title in page


# ---- Adding the columns to an existing database --------------------------


@pytest.mark.parametrize("table, column, column_type", db.LATE_COLUMNS)
def test_init_db_adds_a_missing_column(tmp_path, table, column, column_type):
    """Databases made before archiving existed don't have these columns."""
    old = db.connect(tmp_path / "old.db")
    db.init_db(old)
    old.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    assert column not in columns_of(old, table)

    db.init_db(old)

    assert column in columns_of(old, table)
    old.close()


def columns_of(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
