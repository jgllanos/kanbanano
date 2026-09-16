"""The board version: conflict detection, and what pollers see."""

import pytest

from conftest import version


def card_updated_at(conn, card_id):
    return conn.execute("SELECT updated_at FROM cards WHERE id = ?", (card_id,)).fetchone()[0]


def test_poll_is_a_no_op_while_the_version_matches(client, board):
    response = client.get(f"/boards/{board.id}/poll", params={"v": 0})

    assert response.status_code == 204
    assert response.text == ""


def test_poll_returns_the_board_once_it_has_changed(client, conn, board):
    client.post("/cards", data={"list_id": board.todo, "title": "Paint"})

    response = client.get(f"/boards/{board.id}/poll", params={"v": 0})

    assert response.status_code == 200
    assert "Paint" in response.text
    assert f'data-version="{version(conn, board.id)}"' in response.text


def test_poll_tells_htmx_to_stop_once_the_board_is_gone(client, board):
    client.request("DELETE", f"/boards/{board.id}")

    response = client.get(f"/boards/{board.id}/poll", params={"v": 0})

    assert response.status_code == 286  # htmx stops polling on a 286
    assert "deleted" in response.text


def test_description_edits_are_rejected_if_the_card_changed_underneath(client, conn, board):
    card = board.cards[0]
    stale = card_updated_at(conn, card)
    client.patch(f"/cards/{card}", data={"updated_at": stale, "title": "Tap", "description": "A"})

    response = client.patch(
        f"/cards/{card}", data={"updated_at": stale, "title": "Tap", "description": "B"}
    )

    assert response.status_code == 409
    assert conn.execute("SELECT description FROM cards WHERE id = ?", (card,)).fetchone()[0] == "A"


def test_editing_a_deleted_card_404s(client, conn, board):
    card = board.cards[0]
    stale = card_updated_at(conn, card)
    client.request("DELETE", f"/cards/{card}")

    response = client.patch(
        f"/cards/{card}", data={"updated_at": stale, "title": "Tap", "description": "A"}
    )

    assert response.status_code == 404


@pytest.fixture
def writes(board):
    """Every write path that should bump the board's version, as request kwargs.

    Add new endpoints here. A missing bump means other people's boards don't
    update, with no visible error.
    """
    card = board.cards[0]
    return [
        ("PATCH", f"/boards/{board.id}", {"title": "House"}),
        ("POST", "/lists", {"board_id": board.id, "title": "Doing"}),
        ("PATCH", f"/lists/{board.todo}", {"title": "Backlog"}),
        ("POST", "/cards", {"list_id": board.todo, "title": "Paint"}),
        ("POST", "/cards/reorder", {"list_id": board.todo, "card_ids": board.cards[::-1]}),
        ("POST", "/lists/reorder", {"board_id": board.id, "list_ids": [board.done, board.todo]}),
        ("POST", "/labels", {"card_id": card, "kind": "label", "name": "Urgent", "color": "#f87168"}),
        ("POST", f"/cards/{card}/labels", {"label_id": 1}),
        ("DELETE", f"/cards/{card}/labels/1", None),
        ("PATCH", "/labels/1", {"card_id": card, "name": "Later", "color": "#4bce97"}),
        ("DELETE", f"/labels/1?card_id={card}", None),
        ("POST", f"/cards/{card}/checklist", {"text": "Washer"}),
        ("PATCH", "/checklist/1", {"done": 1}),
        ("DELETE", "/checklist/1", None),
        ("DELETE", f"/cards/{card}", None),
        ("DELETE", f"/lists/{board.todo}", None),
    ]


def test_every_write_bumps_the_version(client, conn, board, writes):
    """Run them in sequence: each one must move the version on by exactly one."""
    for method, url, data in writes:
        before = version(conn, board.id)

        response = client.request(method, url, data=data)

        assert response.is_success, f"{method} {url}: {response.status_code} {response.text}"
        assert version(conn, board.id) == before + 1, f"{method} {url} didn't bump the version"
        assert response.headers["x-board-version"] == str(before + 1)
