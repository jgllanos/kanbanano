"""Drag-and-drop position rewrites.

The invariant throughout: after any reorder, every position in a touched list is
contiguous from 0 and matches the order the client sent. `card_order` and
`list_order` assert the contiguity part.
"""

from conftest import delete_for_good, ordering, version


def card_order(conn, list_id):
    """A list's card ids in order, asserting positions are contiguous from 0."""
    rows = ordering(conn, "cards", "list_id", list_id)
    assert [position for _, position in rows] == [*range(len(rows))]
    return [card_id for card_id, _ in rows]


def list_order(conn, board_id):
    """A board's list ids in order, asserting positions are contiguous from 0."""
    rows = ordering(conn, "lists", "board_id", board_id)
    assert [position for _, position in rows] == [*range(len(rows))]
    return [list_id for list_id, _ in rows]


def reorder(client, list_id, card_ids, from_list_id=None, from_card_ids=()):
    data = {"list_id": list_id, "card_ids": card_ids}
    if from_list_id is not None:
        data |= {"from_list_id": from_list_id, "from_card_ids": list(from_card_ids)}
    return client.post("/cards/reorder", data=data)


def test_reorder_within_a_list(client, conn, board):
    tap, bins, shelf = board.cards

    reorder(client, board.todo, [shelf, tap, bins])

    assert card_order(conn, board.todo) == [shelf, tap, bins]


def test_move_a_card_to_another_list(client, conn, board):
    tap, bins, shelf = board.cards

    reorder(client, board.done, [bins], from_list_id=board.todo, from_card_ids=[tap, shelf])

    assert card_order(conn, board.todo) == [tap, shelf]
    assert card_order(conn, board.done) == [bins]


def test_cards_the_client_did_not_know_about_keep_their_place(client, conn, board):
    """A card someone else added keeps its place after our reorder."""
    tap, bins, shelf = board.cards
    client.post("/cards", data={"list_id": board.todo, "title": "Paint"})
    (paint,) = conn.execute("SELECT id FROM cards WHERE title = 'Paint'").fetchone()

    # The dragging client's page predates "Paint", so it sends three ids.
    reorder(client, board.todo, [shelf, bins, tap])

    assert card_order(conn, board.todo) == [shelf, bins, tap, paint]


def test_ids_for_deleted_cards_are_ignored(client, conn, board):
    tap, bins, shelf = board.cards
    delete_for_good(client, "cards", bins)

    reorder(client, board.todo, [shelf, bins, tap])

    assert card_order(conn, board.todo) == [shelf, tap]


def test_duplicate_ids_are_ignored(client, conn, board):
    tap, bins, shelf = board.cards

    reorder(client, board.todo, [shelf, shelf, tap, bins])

    assert card_order(conn, board.todo) == [shelf, tap, bins]


def test_cards_cannot_be_moved_to_another_board(client, conn, board):
    client.post("/boards", data={"title": "Work"})
    (other_board,) = conn.execute("SELECT id FROM boards WHERE title = 'Work'").fetchone()
    client.post("/lists", data={"board_id": other_board, "title": "Inbox"})
    (other_list,) = conn.execute("SELECT id FROM lists WHERE title = 'Inbox'").fetchone()

    response = reorder(
        client, other_list, board.cards, from_list_id=board.todo, from_card_ids=[]
    )

    assert response.status_code == 400
    assert card_order(conn, board.todo) == board.cards
    assert card_order(conn, other_list) == []


def test_reordering_into_a_deleted_list_404s(client, conn, board):
    delete_for_good(client, "lists", board.done)

    response = reorder(client, board.done, board.cards, from_list_id=board.todo)

    assert response.status_code == 404
    assert card_order(conn, board.todo) == board.cards


def test_reorder_lists(client, conn, board):
    response = client.post(
        "/lists/reorder", data={"board_id": board.id, "list_ids": [board.done, board.todo]}
    )

    assert response.status_code == 204
    assert list_order(conn, board.id) == [board.done, board.todo]


def test_reorder_bumps_the_version_once(client, conn, board):
    before = version(conn, board.id)
    tap, bins, shelf = board.cards

    reorder(client, board.done, [tap], from_list_id=board.todo, from_card_ids=[bins, shelf])

    assert version(conn, board.id) == before + 1


def test_a_delete_leaves_a_gap_that_the_next_reorder_closes(client, conn, board):
    """Deletes don't renumber. A gap doesn't change the order, and renumbering
    would rewrite every sibling on each delete."""
    tap, bins, shelf = board.cards
    delete_for_good(client, "cards", bins)

    assert ordering(conn, "cards", "list_id", board.todo) == [(tap, 0), (shelf, 2)]

    reorder(client, board.todo, [tap, shelf])

    assert card_order(conn, board.todo) == [tap, shelf]  # asserts contiguity too


def test_moves_do_not_touch_updated_at(client, conn, board):
    """updated_at is the conflict token for description edits, so moves leave it alone."""
    tap, bins, shelf = board.cards
    before = conn.execute("SELECT updated_at FROM cards WHERE id = ?", (tap,)).fetchone()[0]

    reorder(client, board.done, [tap], from_list_id=board.todo, from_card_ids=[bins, shelf])

    after = conn.execute("SELECT updated_at FROM cards WHERE id = ?", (tap,)).fetchone()[0]
    assert after == before
