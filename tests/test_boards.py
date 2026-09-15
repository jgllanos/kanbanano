"""Board and list CRUD."""

from conftest import card_order, list_order, version


def test_pages_render(client, board):
    """A smoke test: enough to catch a template that no longer compiles."""
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
