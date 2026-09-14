"""Fill board.db with sample boards for local development.

Usage: uv run seed.py [--reset]

Refuses to touch a database that already has boards unless --reset is given,
in which case all existing boards (and everything on them) are deleted first.
"""

import sys

from app import db

BOARDS = {
    "Home": {
        "background": "#0079bf",
        "lists": {
            "To do": [
                "Fix leaky kitchen tap",
                "Book boiler service",
                "Sort out the garage shelves, and while you're in there find the "
                "missing bike pump and the box of Christmas lights",
            ],
            "Doing": ["Paint spare room", "Renew car insurance"],
            "Done": ["Replace smoke alarm batteries"],
            "Someday": [],
        },
    },
    "Garden": {
        "background": "#519839",
        "lists": {
            "Ideas": ["Raised bed by the fence", "Herb planter"],
            "This season": ["Plant garlic", "Prune apple tree", "Order compost"],
        },
    },
}


def main() -> None:
    conn = db.connect()
    db.init_db(conn)

    has_boards = conn.execute("SELECT 1 FROM boards LIMIT 1").fetchone()
    if has_boards and "--reset" not in sys.argv:
        sys.exit(f"{db.DB_PATH} already has boards; pass --reset to wipe and reseed.")

    with conn:  # one transaction
        conn.execute("DELETE FROM boards")  # cascades to everything else
        for board_title, spec in BOARDS.items():
            board_id = conn.execute(
                "INSERT INTO boards (title, background) VALUES (?, ?)",
                (board_title, spec["background"]),
            ).lastrowid
            for list_pos, (list_title, cards) in enumerate(spec["lists"].items()):
                list_id = conn.execute(
                    "INSERT INTO lists (board_id, title, position) VALUES (?, ?, ?)",
                    (board_id, list_title, list_pos),
                ).lastrowid
                conn.executemany(
                    "INSERT INTO cards (list_id, title, position) VALUES (?, ?, ?)",
                    [(list_id, title, pos) for pos, title in enumerate(cards)],
                )
    conn.close()
    print(f"Seeded {len(BOARDS)} boards into {db.DB_PATH}")


if __name__ == "__main__":
    main()
