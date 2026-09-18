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
        "labels": {"Urgent": "red", "Errand": "blue", "DIY": "orange"},
        "people": ["Alex Smith", "Sam"],
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
        # Card title -> label and person names to assign.
        "assign": {
            "Fix leaky kitchen tap": ["DIY", "Alex Smith"],
            "Book boiler service": ["Urgent"],
            "Paint spare room": ["DIY", "Alex Smith", "Sam"],
            "Renew car insurance": ["Urgent", "Errand", "Sam"],
        },
        "descriptions": {
            "Paint spare room": "Colour: Pale Sage, 2 coats.\nNeed a new roller and dust sheets.",
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

    palette = list(db.LABEL_COLORS.values())
    with conn:  # one transaction
        conn.execute("DELETE FROM boards")  # cascades to everything else
        for board_title, spec in BOARDS.items():
            board_id = conn.execute(
                "INSERT INTO boards (title, background) VALUES (?, ?)",
                (board_title, spec["background"]),
            ).lastrowid

            label_ids = {}
            for name, color in spec.get("labels", {}).items():
                label_ids[name] = conn.execute(
                    "INSERT INTO labels (board_id, name, color, kind) VALUES (?, ?, ?, 'label')",
                    (board_id, name, db.LABEL_COLORS[color]),
                ).lastrowid
            for i, name in enumerate(spec.get("people", [])):
                # Same rule as the app: each new person gets the next palette color.
                label_ids[name] = conn.execute(
                    "INSERT INTO labels (board_id, name, color, kind) VALUES (?, ?, ?, 'person')",
                    (board_id, name, palette[i % len(palette)]),
                ).lastrowid

            for list_pos, (list_title, cards) in enumerate(spec["lists"].items()):
                list_id = conn.execute(
                    "INSERT INTO lists (board_id, title, position) VALUES (?, ?, ?)",
                    (board_id, list_title, list_pos),
                ).lastrowid
                for card_pos, title in enumerate(cards):
                    card_id = conn.execute(
                        """
                        INSERT INTO cards (list_id, title, description, position)
                        VALUES (?, ?, ?, ?)
                        """,
                        (list_id, title, spec.get("descriptions", {}).get(title, ""), card_pos),
                    ).lastrowid
                    conn.executemany(
                        "INSERT INTO card_labels (card_id, label_id) VALUES (?, ?)",
                        [
                            (card_id, label_ids[name])
                            for name in spec.get("assign", {}).get(title, [])
                        ],
                    )
    conn.close()
    print(f"Seeded {len(BOARDS)} boards into {db.DB_PATH}")


if __name__ == "__main__":
    main()
