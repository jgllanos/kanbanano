"""Checklist items, and the done/total count the card face shows."""

import pytest

from conftest import delete_for_good, ordering, version


def item_ids(conn, card_id) -> list[int]:
    """A card's checklist item ids, in the order the modal renders them."""
    return [item_id for item_id, _ in ordering(conn, "checklist_items", "card_id", card_id)]


def progress(conn, card_id) -> tuple[int, int]:
    """The (done, total) the card face shows."""
    row = conn.execute(
        "SELECT COALESCE(SUM(done), 0) AS done, COUNT(*) AS total "
        "FROM checklist_items WHERE card_id = ?",
        (card_id,),
    ).fetchone()
    return row["done"], row["total"]


@pytest.fixture
def card(client, conn, board):
    """A card carrying a three-item checklist, the first of them done."""
    card_id = board.cards[0]
    for text in ["Washer", "Spanner", "Tap"]:
        client.post(f"/cards/{card_id}/checklist", data={"text": text})
    client.patch(f"/checklist/{item_ids(conn, card_id)[0]}", data={"done": 1})
    return card_id


def test_items_are_appended_in_order(client, conn, board):
    card_id = board.cards[0]

    for text in ["Washer", "Spanner"]:
        client.post(f"/cards/{card_id}/checklist", data={"text": text})

    stored = conn.execute(
        "SELECT text FROM checklist_items WHERE card_id = ? ORDER BY position, id", (card_id,)
    ).fetchall()
    assert [row["text"] for row in stored] == ["Washer", "Spanner"]
    assert ordering(conn, "checklist_items", "card_id", card_id) == [(1, 0), (2, 1)]


def test_a_blank_item_is_rejected(client, conn, board):
    response = client.post(f"/cards/{board.cards[0]}/checklist", data={"text": "  "})

    assert response.status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM checklist_items").fetchone()[0] == 0


def test_adding_an_item_to_a_deleted_card_404s(client, board):
    card_id = board.cards[0]
    delete_for_good(client, "cards", card_id)

    assert client.post(f"/cards/{card_id}/checklist", data={"text": "X"}).status_code == 404


def test_ticking_sends_the_value_to_set_not_a_toggle(client, conn, card):
    """Two clients ticking the same item both leave it done."""
    item = item_ids(conn, card)[1]

    client.patch(f"/checklist/{item}", data={"done": 1})
    client.patch(f"/checklist/{item}", data={"done": 1})  # the second, stale client

    assert progress(conn, card) == (2, 3)


def test_editing_text_leaves_done_alone(client, conn, card):
    done_item = item_ids(conn, card)[0]

    client.patch(f"/checklist/{done_item}", data={"text": "  Rubber   washer "})

    row = conn.execute(
        "SELECT text, done FROM checklist_items WHERE id = ?", (done_item,)
    ).fetchone()
    assert row["text"] == "Rubber washer"  # whitespace collapsed
    assert row["done"] == 1


def test_unticking_leaves_text_alone(client, conn, card):
    done_item = item_ids(conn, card)[0]

    client.patch(f"/checklist/{done_item}", data={"done": 0})

    row = conn.execute(
        "SELECT text, done FROM checklist_items WHERE id = ?", (done_item,)
    ).fetchone()
    assert (row["text"], row["done"]) == ("Washer", 0)


def test_delete_an_item(client, conn, card):
    item = item_ids(conn, card)[1]

    client.request("DELETE", f"/checklist/{item}")

    assert item not in item_ids(conn, card)
    assert progress(conn, card) == (1, 2)


def test_a_deleted_item_404s(client, conn, card):
    item = item_ids(conn, card)[1]
    client.request("DELETE", f"/checklist/{item}")

    assert client.patch(f"/checklist/{item}", data={"done": 1}).status_code == 404
    assert client.request("DELETE", f"/checklist/{item}").status_code == 404


def test_deleting_a_card_takes_its_checklist_with_it(client, conn, card):
    delete_for_good(client, "cards", card)

    assert conn.execute("SELECT COUNT(*) FROM checklist_items").fetchone()[0] == 0


def test_every_checklist_write_bumps_the_version(client, conn, board):
    card_id = board.cards[0]
    before = version(conn, board.id)

    client.post(f"/cards/{card_id}/checklist", data={"text": "Washer"})
    assert version(conn, board.id) == before + 1

    item = item_ids(conn, card_id)[0]
    client.patch(f"/checklist/{item}", data={"done": 1})
    assert version(conn, board.id) == before + 2

    client.request("DELETE", f"/checklist/{item}")
    assert version(conn, board.id) == before + 3


# ---- Rendered output ------------------------------------------------------
# These check the markup: that the count appears on the board, and that a write
# updates the card face as well as the modal.


def test_the_board_shows_progress_on_the_card_face(client, conn, board, card):
    board_html = client.get(f"/boards/{board.id}").text
    assert "1/3" in board_html
    assert "is-complete" not in board_html

    for item in item_ids(conn, card)[1:]:
        client.patch(f"/checklist/{item}", data={"done": 1})

    assert "is-complete" in client.get(f"/boards/{board.id}").text


def test_a_write_also_returns_the_card_face(client, conn, card):
    response = client.patch(f"/checklist/{item_ids(conn, card)[1]}", data={"done": 1})

    assert f'hx-swap-oob="innerHTML:#card-{card}"' in response.text
    assert "2/3" in response.text


def test_the_modal_renders_without_an_out_of_band_face(client, conn, card):
    """Only writes swap the card face; opening a card doesn't."""
    modal = client.get(f"/cards/{card}").text

    assert "Washer" in modal
    assert "hx-swap-oob" not in modal
