"""Board and list CRUD, and board backgrounds."""

import pytest

from app import db
from app.main import needs_dark_ink
from conftest import card_order, list_order, version


def test_pages_render(client, board):
    """Smoke test for template errors."""
    index = client.get("/")
    assert index.status_code == 200 and "Create board" in index.text

    view = client.get(f"/boards/{board.id}")
    assert view.status_code == 200
    for expected in ["Home", "To do", "Tap", "Add a list", "Delete board"]:
        assert expected in view.text


def test_create_board_redirects_to_it(client, conn):
    response = client.post("/boards", data={"title": "  Weekend   plans "})

    board_id, title = conn.execute("SELECT id, title FROM boards").fetchone()
    assert title == "Weekend plans"  # whitespace collapsed
    assert response.headers["hx-redirect"] == f"/boards/{board_id}"


def test_create_board_rejects_a_blank_title(client, conn):
    response = client.post("/boards", data={"title": "   "})

    assert response.status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 0


def test_rename_board(client, conn, board):
    response = client.patch(f"/boards/{board.id}", data={"title": "House"})

    assert conn.execute("SELECT title FROM boards").fetchone()["title"] == "House"
    assert "House" in response.text  # the re-rendered header


def test_rename_board_bumps_the_version(client, conn, board):
    before = version(conn, board.id)

    response = client.patch(f"/boards/{board.id}", data={"title": "House"})

    assert version(conn, board.id) == before + 1
    assert response.headers["x-board-version"] == str(before + 1)


def test_set_the_background(client, conn, board):
    before = version(conn, board.id)

    response = client.patch(f"/boards/{board.id}", data={"background": "#b04632"})

    assert conn.execute("SELECT background FROM boards").fetchone()[0] == "#b04632"
    assert version(conn, board.id) == before + 1
    # Only the menu is returned, so it stays open.
    assert 'id="board-menu-panel"' in response.text


def test_the_background_reaches_the_page(client, board):
    client.patch(f"/boards/{board.id}", data={"background": "#b04632"})

    assert "--board-bg: #b04632" in client.get(f"/boards/{board.id}").text
    assert "#b04632" in client.get("/").text  # the tile on the index


def test_any_hex_color_is_accepted_and_lower_cased(client, conn, board):
    client.patch(f"/boards/{board.id}", data={"background": "#A1B2C3"})

    assert conn.execute("SELECT background FROM boards").fetchone()[0] == "#a1b2c3"


def test_anything_but_a_hex_color_is_rejected(client, conn, board):
    """Backgrounds go into a stylesheet, where HTML escaping doesn't help."""
    bad = [
        "red",
        "#fff",  # valid CSS, but the color input never sends shorthand
        "#12345",
        "#1234567",
        "#12345g",
        "#0079bf\n",
        "#0079bf; } body { display: none } .x {",
        "url(javascript:alert(1))",
    ]
    for value in bad:
        response = client.patch(f"/boards/{board.id}", data={"background": value})
        assert response.status_code == 400, repr(value)

    assert conn.execute("SELECT background FROM boards").fetchone()[0] == "#0079bf"
    assert version(conn, board.id) == 0


@pytest.mark.parametrize(
    "background, dark",
    [
        *((hex_, False) for hex_ in db.BOARD_COLORS.values()),  # every preset keeps white text
        ("#000000", False),
        ("#ffffff", True),
        ("#ffff00", True),  # yellow
        ("#ffb3c6", True),  # a pastel pink
        ("#8a8a8a", False),  # mid grey: white still clears 3:1
    ],
)
def test_needs_dark_ink(background, dark):
    assert needs_dark_ink(background) is dark


def test_a_light_background_gets_dark_text(client, board):
    client.patch(f"/boards/{board.id}", data={"background": "#ffff00"})

    assert "--board-ink: 23 43 77" in client.get(f"/boards/{board.id}").text
    assert "dark-ink" in client.get("/").text  # the tile on the index


def test_renaming_and_recoloring_leave_each_other_alone(client, conn, board):
    client.patch(f"/boards/{board.id}", data={"background": "#519839"})
    client.patch(f"/boards/{board.id}", data={"title": "House"})

    stored = conn.execute("SELECT title, background FROM boards").fetchone()
    assert (stored["title"], stored["background"]) == ("House", "#519839")


def test_delete_board_takes_its_lists_and_cards_with_it(client, conn, board):
    response = client.request("DELETE", f"/boards/{board.id}")

    assert response.headers["hx-redirect"] == "/"
    for table in ["boards", "lists", "cards"]:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_a_deleted_board_404s(client, conn, board):
    client.request("DELETE", f"/boards/{board.id}")

    assert client.get(f"/boards/{board.id}").status_code == 404
    assert client.patch(f"/boards/{board.id}", data={"title": "House"}).status_code == 404
    assert client.post("/lists", data={"board_id": board.id, "title": "New"}).status_code == 404


def test_create_list_appends_it_to_the_board(client, conn, board):
    response = client.post("/lists", data={"board_id": board.id, "title": "Doing"})

    assert response.status_code == 200
    new_id = conn.execute("SELECT id FROM lists WHERE title = 'Doing'").fetchone()["id"]
    assert list_order(conn, board.id) == [board.todo, board.done, new_id]


def test_create_list_bumps_the_version(client, conn, board):
    before = version(conn, board.id)

    client.post("/lists", data={"board_id": board.id, "title": "Doing"})

    assert version(conn, board.id) == before + 1


def test_rename_list(client, conn, board):
    before = version(conn, board.id)

    response = client.patch(f"/lists/{board.todo}", data={"title": "Backlog"})

    stored = conn.execute("SELECT title FROM lists WHERE id = ?", (board.todo,)).fetchone()
    assert stored["title"] == "Backlog"
    assert "Backlog" in response.text  # the re-rendered header
    assert version(conn, board.id) == before + 1


def test_rename_list_rejects_a_blank_title(client, conn, board):
    response = client.patch(f"/lists/{board.todo}", data={"title": " \n "})

    assert response.status_code == 400
    assert version(conn, board.id) == 0  # nothing happened, so nobody needs to refetch


def test_delete_list_takes_its_cards_with_it(client, conn, board):
    before = version(conn, board.id)

    response = client.request("DELETE", f"/lists/{board.todo}")

    assert response.status_code == 200
    assert list_order(conn, board.id) == [board.done]
    assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
    assert version(conn, board.id) == before + 1


def test_a_deleted_list_404s(client, conn, board):
    client.request("DELETE", f"/lists/{board.todo}")

    assert client.patch(f"/lists/{board.todo}", data={"title": "Backlog"}).status_code == 404
    assert client.request("DELETE", f"/lists/{board.todo}").status_code == 404
    assert client.post("/cards", data={"list_id": board.todo, "title": "X"}).status_code == 404


def test_create_card_appends_it_to_the_list(client, conn, board):
    client.post("/cards", data={"list_id": board.todo, "title": "Paint"})

    new_id = conn.execute("SELECT id FROM cards WHERE title = 'Paint'").fetchone()["id"]
    assert card_order(conn, board.todo) == [*board.cards, new_id]
