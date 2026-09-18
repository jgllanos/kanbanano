"""Reading and shaping the data the templates render.

Everything here takes a connection and returns rows or plain dicts. Nothing here
opens a transaction or bumps the board version — that's the routes' job, so that
a whole write stays visible in one place.

The load_* functions raise 404 for a row that's been deleted, because every
caller wants that and a page open since before the delete is the normal way to
get here.
"""

import json
import sqlite3
from collections.abc import Iterable

from fastapi import HTTPException


def clean_text(value: str, what: str) -> str:
    """Collapse whitespace, including pasted newlines, to single spaces.

    Raises 400 if nothing is left. `what` names the field in the error message.
    """
    value = " ".join(value.split())
    if not value:
        raise HTTPException(status_code=400, detail=f"{what} can't be empty")
    return value


def id_list(ids: Iterable[int]) -> str:
    """Ids as JSON, for the `IN (SELECT value FROM json_each(?))` pattern.

    SQLite has no array parameter, and building an IN list by hand would mean
    interpolating the ids into the SQL. json_each keeps them parameters.
    """
    return json.dumps(list(ids))


# The attach_* pair fills in columns that need a second query. Both mutate the
# card dicts in place and return nothing, so a caller can't be in doubt about
# which copy is the up-to-date one.


def attach_labels(conn: sqlite3.Connection, cards: list[dict]) -> None:
    """Give each card dict `labels` and `people` lists, in creation order. One query."""
    by_id = {}
    for card in cards:
        card["labels"], card["people"] = [], []
        by_id[card["id"]] = card
    for row in conn.execute(
        """
        SELECT card_labels.card_id, labels.id, labels.name, labels.color, labels.kind
        FROM card_labels JOIN labels ON labels.id = card_labels.label_id
        WHERE card_labels.card_id IN (SELECT value FROM json_each(?))
        ORDER BY labels.id
        """,
        (id_list(by_id),),
    ):
        by_id[row["card_id"]]["people" if row["kind"] == "person" else "labels"].append(row)


def attach_progress(conn: sqlite3.Connection, cards: list[dict]) -> None:
    """Give each card dict `checklist_done` and `checklist_total`. One query.

    These are the counts shown on the card face. The modal loads the items
    themselves with load_checklist.
    """
    by_id = {}
    for card in cards:
        card["checklist_done"], card["checklist_total"] = 0, 0
        by_id[card["id"]] = card
    for row in conn.execute(
        """
        SELECT card_id, SUM(done) AS done, COUNT(*) AS total
        FROM checklist_items
        WHERE card_id IN (SELECT value FROM json_each(?))
        GROUP BY card_id
        """,
        (id_list(by_id),),
    ):
        by_id[row["card_id"]]["checklist_done"] = row["done"]
        by_id[row["card_id"]]["checklist_total"] = row["total"]


# ---- Boards, lists and cards ----------------------------------------------


def load_board(conn: sqlite3.Connection, board_id: int) -> sqlite3.Row:
    """One board. 404 if it doesn't exist."""
    board = conn.execute("SELECT * FROM boards WHERE id = ?", (board_id,)).fetchone()
    if board is None:
        raise HTTPException(status_code=404, detail="This board was deleted")
    return board


def load_list(conn: sqlite3.Connection, list_id: int) -> sqlite3.Row:
    """One list. 404 if it's been deleted."""
    lst = conn.execute("SELECT * FROM lists WHERE id = ?", (list_id,)).fetchone()
    if lst is None:
        raise HTTPException(status_code=404, detail="This list was deleted")
    return lst


def load_lists(conn: sqlite3.Connection, board_id: int) -> list[dict]:
    """A board's lists in order, each with its cards in order. Four queries total."""
    lists = [
        dict(row)
        for row in conn.execute(
            """
            SELECT id, title FROM lists
            WHERE board_id = ? AND archived_at IS NULL
            ORDER BY position, id
            """,
            (board_id,),
        )
    ]
    cards = [
        dict(row)
        for row in conn.execute(
            """
            SELECT cards.id, cards.list_id, cards.title, cards.description != '' AS has_description
            FROM cards JOIN lists ON lists.id = cards.list_id
            WHERE lists.board_id = ? AND lists.archived_at IS NULL AND cards.archived_at IS NULL
            ORDER BY cards.position, cards.id
            """,
            (board_id,),
        )
    ]
    attach_labels(conn, cards)
    attach_progress(conn, cards)
    cards_by_list = {lst["id"]: [] for lst in lists}
    for card in cards:
        cards_by_list[card["list_id"]].append(card)
    for lst in lists:
        lst["cards"] = cards_by_list[lst["id"]]
    return lists


# What _card_face.html draws. The description itself isn't among it — only
# whether there is one — so a face doesn't carry one around.
FACE_COLUMNS = """
    cards.id, cards.list_id, cards.title, cards.description != '' AS has_description,
    lists.board_id, lists.title AS list_title
"""


def load_faces(conn: sqlite3.Connection, card_ids: Iterable[int]) -> list[dict]:
    """Cards with what their face needs. Deleted ids are skipped.

    Renaming or deleting a label re-renders the face of every card carrying it,
    which is why this stays narrow.
    """
    rows = conn.execute(
        f"""
        SELECT {FACE_COLUMNS}
        FROM cards JOIN lists ON lists.id = cards.list_id
        WHERE cards.id IN (SELECT value FROM json_each(?))
        """,
        (id_list(card_ids),),
    )
    cards = [dict(row) for row in rows]
    attach_labels(conn, cards)
    attach_progress(conn, cards)
    return cards


def load_card(conn: sqlite3.Connection, card_id: int) -> dict:
    """One card in full: its face, plus the description and updated_at that the
    modal and the writes need. 404 if it's been deleted."""
    row = conn.execute(
        """
        SELECT cards.*, cards.description != '' AS has_description,
               lists.board_id, lists.title AS list_title
        FROM cards JOIN lists ON lists.id = cards.list_id
        WHERE cards.id = ?
        """,
        (card_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="This card was deleted")
    card = dict(row)
    attach_labels(conn, [card])
    attach_progress(conn, [card])
    return card


def index_tiles(conn: sqlite3.Connection) -> dict[str, list]:
    """The index's boards, and its archived ones for the section below them."""
    boards = conn.execute(
        """
        SELECT id, title, background FROM boards
        WHERE archived_at IS NULL ORDER BY created_at, id
        """
    ).fetchall()
    archived = conn.execute(
        """
        SELECT id, title, background FROM boards
        WHERE archived_at IS NOT NULL ORDER BY archived_at DESC, id DESC
        """
    ).fetchall()
    return {"boards": boards, "archived": archived}


# ---- Ordering -------------------------------------------------------------


def rewrite_positions(
    conn: sqlite3.Connection,
    table: str,
    parent_column: str,
    parent_id: int,
    ordered_ids: list[int],
    exclude: Iterable[int] = (),
) -> None:
    """Renumber a parent's children 0, 1, 2…, starting with `ordered_ids` in order.

    `ordered_ids` are moved under the parent if needed; the caller must check
    they're on the same board. Children not in `ordered_ids` (for example, added
    by someone else since the page loaded) go after them in their current order.
    `exclude` holds ids the same request is moving to another parent.

    Every drag writes positions through here. Moves don't change
    cards.updated_at, which is the conflict token for description edits.
    """
    skip = {*ordered_ids, *exclude}
    leftovers = [
        row["id"]
        for row in conn.execute(
            f"SELECT id FROM {table} WHERE {parent_column} = ? ORDER BY position, id",
            (parent_id,),
        )
        if row["id"] not in skip
    ]
    conn.executemany(
        f"UPDATE {table} SET {parent_column} = ?, position = ? WHERE id = ?",
        [(parent_id, position, id_) for position, id_ in enumerate(ordered_ids + leftovers)],
    )


# ---- Archiving ------------------------------------------------------------

# The same clock as the schema's created_at and updated_at defaults.
NOW = "strftime('%Y-%m-%d %H:%M:%f', 'now')"

ARCHIVABLE = ("boards", "lists", "cards")


def archive(conn: sqlite3.Connection, table: str, row_id: int) -> None:
    """Mark a row archived, now. Leaves an already-archived row alone, so its
    archived_at stays the moment it was archived rather than the last attempt."""
    assert table in ARCHIVABLE  # the table name is interpolated, so pin it to these
    conn.execute(
        f"UPDATE {table} SET archived_at = {NOW} WHERE id = ? AND archived_at IS NULL",
        (row_id,),
    )


def require_archived(row: sqlite3.Row, what: str) -> None:
    """Refuse to delete something that's still in use.

    Deleting for good is only offered from the archive, but a page open since
    before someone restored it would still show the button. Archiving is the one
    way in, so the check belongs here rather than only in the template.
    """
    if row["archived_at"] is None:
        raise HTTPException(status_code=409, detail=f"Archive this {what} before deleting it")


def load_archive(conn: sqlite3.Connection, board_id: int) -> dict[str, list]:
    """A board's archived lists and cards, newest first.

    Archiving a list doesn't archive its cards: they stay in it and come back
    with it, so they aren't listed separately. Each archived list shows how many
    cards a restore would bring back (`card_count`), and how many a delete would
    take (`total_count`, which includes any archived on their own beforehand and
    so listed below as well).
    """
    lists = conn.execute(
        """
        SELECT id, title, archived_at,
               (SELECT COUNT(*) FROM cards
                WHERE cards.list_id = lists.id AND cards.archived_at IS NULL) AS card_count,
               (SELECT COUNT(*) FROM cards WHERE cards.list_id = lists.id) AS total_count
        FROM lists
        WHERE board_id = ? AND archived_at IS NOT NULL
        ORDER BY archived_at DESC, id DESC
        """,
        (board_id,),
    ).fetchall()
    cards = conn.execute(
        """
        SELECT cards.id, cards.title, cards.archived_at, lists.title AS list_title
        FROM cards JOIN lists ON lists.id = cards.list_id
        WHERE lists.board_id = ? AND cards.archived_at IS NOT NULL
        ORDER BY cards.archived_at DESC, cards.id DESC
        """,
        (board_id,),
    ).fetchall()
    return {"lists": lists, "cards": cards}


# ---- Labels and checklists ------------------------------------------------


def board_labels(conn: sqlite3.Connection, board_id: int) -> dict[str, list]:
    """All of a board's labels, split by kind: {"label": [...], "person": [...]}."""
    options = {"label": [], "person": []}
    for row in conn.execute(
        "SELECT id, name, color, kind FROM labels WHERE board_id = ? ORDER BY id", (board_id,)
    ):
        options[row["kind"]].append(row)
    return options


def get_label(conn: sqlite3.Connection, label_id: int, board_id: int) -> sqlite3.Row:
    label = conn.execute(
        "SELECT id, kind FROM labels WHERE id = ? AND board_id = ?", (label_id, board_id)
    ).fetchone()
    if label is None:
        raise HTTPException(status_code=404, detail="This label was deleted")
    return label


def cards_with_label(conn: sqlite3.Connection, label_id: int) -> list[int]:
    return [
        row["card_id"]
        for row in conn.execute("SELECT card_id FROM card_labels WHERE label_id = ?", (label_id,))
    ]


def load_checklist(conn: sqlite3.Connection, card_id: int) -> list[sqlite3.Row]:
    """A card's checklist items, in order."""
    return conn.execute(
        "SELECT * FROM checklist_items WHERE card_id = ? ORDER BY position, id", (card_id,)
    ).fetchall()


def load_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row:
    """One checklist item, plus its board_id for the version bump."""
    item = conn.execute(
        """
        SELECT checklist_items.*, lists.board_id
        FROM checklist_items
        JOIN cards ON cards.id = checklist_items.card_id
        JOIN lists ON lists.id = cards.list_id
        WHERE checklist_items.id = ?
        """,
        (item_id,),
    ).fetchone()
    if item is None:
        raise HTTPException(status_code=404, detail="This checklist item was deleted")
    return item
