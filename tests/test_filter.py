"""The board filter's server side: new cards get the filter's labels and people,
and the header lists every label and person for the filter to offer.

The filtering itself happens in the browser (see the filter store in app.js).
"""

import pytest

from conftest import version


@pytest.fixture
def labels(conn, board):
    """A label and a person on the board, and a label on another board."""
    with conn:
        insert = "INSERT INTO labels (board_id, name, color, kind) VALUES (?, ?, ?, ?)"
        house = conn.execute(insert, (board.id, "House", "#6cc3e0", "label")).lastrowid
        juan = conn.execute(insert, (board.id, "Juan", "#4bce97", "person")).lastrowid
        other_board = conn.execute("INSERT INTO boards (title) VALUES ('Work')").lastrowid
        elsewhere = conn.execute(insert, (other_board, "Urgent", "#f87168", "label")).lastrowid
    return {"house": house, "juan": juan, "elsewhere": elsewhere}


def assigned(conn, title):
    return {
        row["label_id"]
        for row in conn.execute(
            """
            SELECT label_id FROM card_labels JOIN cards ON cards.id = card_labels.card_id
            WHERE cards.title = ?
            """,
            (title,),
        )
    }


def test_a_new_card_gets_the_labels_and_people_sent_with_it(client, conn, board, labels):
    response = client.post(
        "/cards",
        data={
            "list_id": board.todo,
            "title": "Gate",
            "label_ids": [labels["house"], labels["juan"]],
        },
    )

    assert response.status_code == 200
    assert assigned(conn, "Gate") == {labels["house"], labels["juan"]}
    # The returned card shows them, so it matches the filter straight away.
    assert f'data-label-id="{labels["house"]}"' in response.text
    assert f'data-person-id="{labels["juan"]}"' in response.text


def test_a_new_card_without_labels_gets_none(client, conn, board, labels):
    client.post("/cards", data={"list_id": board.todo, "title": "Gate"})

    assert assigned(conn, "Gate") == set()


def test_labels_from_another_board_or_deleted_are_skipped(client, conn, board, labels):
    response = client.post(
        "/cards",
        data={
            "list_id": board.todo,
            "title": "Gate",
            "label_ids": [labels["house"], labels["elsewhere"], 9999, labels["house"]],
        },
    )

    assert response.status_code == 200
    assert assigned(conn, "Gate") == {labels["house"]}


def test_creating_a_card_with_labels_bumps_the_version_once(client, conn, board, labels):
    before = version(conn, board.id)

    client.post(
        "/cards",
        data={
            "list_id": board.todo,
            "title": "Gate",
            "label_ids": [labels["house"], labels["juan"]],
        },
    )

    assert version(conn, board.id) == before + 1


def test_the_header_offers_every_label_and_person(client, board, labels):
    page = client.get(f"/boards/{board.id}").text

    assert "Clear filter" in page
    assert f"$store.filter.toggle({labels['house']})" in page
    assert f"$store.filter.toggle({labels['juan']})" in page
    assert f"$store.filter.toggle({labels['elsewhere']})" not in page


@pytest.mark.parametrize("kind", ["label", "person"])
def test_adding_a_label_or_person_refreshes_the_filter(client, board, kind):
    """The header's filter lists labels too now, so a new label re-renders it."""
    response = client.post(
        "/labels",
        data={"card_id": board.cards[0], "kind": kind, "name": "Garden", "color": "#94c748"},
    )

    assert 'id="board-filter"' in response.text
    assert "$store.filter.toggle(" in response.text
