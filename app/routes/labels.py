"""Labels and people. They're the same table, told apart by `kind`.

All of these are done from a card's modal, so they take a card and work on its
board, and they answer with that modal's section.
"""

import sqlite3
from collections.abc import Iterable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app import db
from app.queries import (
    board_labels,
    cards_with_label,
    clean_text,
    get_label,
    load_card,
    load_faces,
)
from app.templating import templates
from app.theme import check_color

router = APIRouter()


def label_section(
    request: Request,
    conn: sqlite3.Connection,
    card_id: int,
    kind: str,
    face_ids: Iterable[int],
    *,
    managing: bool = False,
    options_changed: bool = False,
):
    """Re-render the modal's labels or people section after a change.

    Also swaps out-of-band the faces of `face_ids` (every card showing what
    changed) and, if a label or person was added, renamed, recolored or
    deleted, the header's filter, which lists them.
    `managing` keeps the section in its edit view.
    """
    card = load_card(conn, card_id)
    options = board_labels(conn, card["board_id"])
    return templates.TemplateResponse(
        request,
        "_label_section.html",
        {
            "card": card,
            "kind": kind,
            "options": options[kind],
            "board_options": options,
            "managing": managing,
            "faces": load_faces(conn, face_ids),
            "refresh_filter": options_changed,
        },
    )


@router.post("/cards/{card_id}/labels", response_class=HTMLResponse)
def add_card_label(
    card_id: int,
    request: Request,
    label_id: Annotated[int, Form()],
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Assign a label or person to a card.

    This is idempotent instead of a toggle, so a stale modal can't undo someone
    else's change.
    """
    with conn:
        card = load_card(conn, card_id)
        label = get_label(conn, label_id, card["board_id"])
        conn.execute(
            "INSERT OR IGNORE INTO card_labels (card_id, label_id) VALUES (?, ?)",
            (card_id, label_id),
        )
        db.bump_version(conn, card["board_id"])
    return label_section(request, conn, card_id, label["kind"], [card_id])


@router.delete("/cards/{card_id}/labels/{label_id}", response_class=HTMLResponse)
def remove_card_label(
    card_id: int, label_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)
):
    """Unassign a label or person from a card. Idempotent."""
    with conn:
        card = load_card(conn, card_id)
        label = get_label(conn, label_id, card["board_id"])
        conn.execute(
            "DELETE FROM card_labels WHERE card_id = ? AND label_id = ?", (card_id, label_id)
        )
        db.bump_version(conn, card["board_id"])
    return label_section(request, conn, card_id, label["kind"], [card_id])


@router.post("/labels", response_class=HTMLResponse)
def create_label(
    request: Request,
    card_id: Annotated[int, Form()],
    kind: Annotated[Literal["label", "person"], Form()],
    name: Annotated[str, Form()] = "",
    color: Annotated[str, Form()] = "",
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Create a label or person on a card's board and assign it to that card.

    Labels are created from the card modal, so this takes a card and uses its
    board. Labels pick a palette color; people get the next one automatically.
    """
    name = clean_text(name, "Name")
    if kind == "label":
        check_color(color)

    with conn:
        card = load_card(conn, card_id)
        if kind == "person":
            (people,) = conn.execute(
                "SELECT COUNT(*) FROM labels WHERE board_id = ? AND kind = 'person'",
                (card["board_id"],),
            ).fetchone()
            palette = list(db.LABEL_COLORS.values())
            color = palette[people % len(palette)]
        label_id = conn.execute(
            "INSERT INTO labels (board_id, name, color, kind) VALUES (?, ?, ?, ?)",
            (card["board_id"], name, color, kind),
        ).lastrowid
        conn.execute(
            "INSERT INTO card_labels (card_id, label_id) VALUES (?, ?)", (card_id, label_id)
        )
        db.bump_version(conn, card["board_id"])
    return label_section(request, conn, card_id, kind, [card_id], options_changed=True)


@router.patch("/labels/{label_id}", response_class=HTMLResponse)
def update_label(
    label_id: int,
    request: Request,
    card_id: Annotated[int, Form()],
    name: Annotated[str, Form()] = "",
    color: Annotated[str, Form()] = "",
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Rename or recolor a label or person.

    Like POST /labels this is done from a card's modal, so it takes the card:
    the response is that modal's section, plus every card face showing the label.
    """
    name = clean_text(name, "Name")
    check_color(color)
    with conn:
        card = load_card(conn, card_id)
        label = get_label(conn, label_id, card["board_id"])
        conn.execute("UPDATE labels SET name = ?, color = ? WHERE id = ?", (name, color, label_id))
        db.bump_version(conn, card["board_id"])
    return label_section(
        request,
        conn,
        card_id,
        label["kind"],
        cards_with_label(conn, label_id),
        managing=True,
        options_changed=True,
    )


@router.delete("/labels/{label_id}", response_class=HTMLResponse)
def delete_label(
    label_id: int, card_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)
):
    """Delete a label or person, removing it from every card. `card_id` (a query
    parameter) is the card whose modal this was done from, as for PATCH."""
    with conn:
        card = load_card(conn, card_id)
        label = get_label(conn, label_id, card["board_id"])
        affected = cards_with_label(conn, label_id)  # read before the cascade clears them
        conn.execute("DELETE FROM labels WHERE id = ?", (label_id,))
        db.bump_version(conn, card["board_id"])
    return label_section(
        request, conn, card_id, label["kind"], affected, managing=True, options_changed=True
    )
