"""A card's checklist. Every write answers with the whole checklist, plus the
card face out-of-band, since the face shows the same done/total count."""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app import db
from app.queries import clean_text, load_card, load_checklist, load_item
from app.templating import templates

router = APIRouter()


def checklist_body(request: Request, conn: sqlite3.Connection, card_id: int):
    """The response to every checklist write.

    Re-renders the modal's checklist, plus the card face out-of-band, since it
    shows the same done/total count.
    """
    return templates.TemplateResponse(
        request,
        "_checklist_saved.html",
        {"card": load_card(conn, card_id), "items": load_checklist(conn, card_id)},
    )


@router.post("/cards/{card_id}/checklist", response_class=HTMLResponse)
def add_checklist_item(
    card_id: int,
    request: Request,
    text: Annotated[str, Form()],
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Append an item to a card's checklist."""
    text = clean_text(text, "Checklist item")
    with conn:
        card = load_card(conn, card_id)
        conn.execute(
            """
            INSERT INTO checklist_items (card_id, text, position)
            VALUES (?, ?, (SELECT COALESCE(MAX(position) + 1, 0) FROM checklist_items
                           WHERE card_id = ?))
            """,
            (card_id, text, card_id),
        )
        db.bump_version(conn, card["board_id"])
    return checklist_body(request, conn, card_id)


@router.patch("/checklist/{item_id}", response_class=HTMLResponse)
def update_checklist_item(
    item_id: int,
    request: Request,
    done: Annotated[bool | None, Form()] = None,
    text: Annotated[str | None, Form()] = None,
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Tick an item or edit its text. Send whichever one changed.

    `done` is the value to set. A toggle would let a stale modal undo someone
    else's change; the label endpoints avoid toggles for the same reason.
    """
    if done is None and text is None:
        raise HTTPException(status_code=400, detail="That save was empty")
    if text is not None:
        text = clean_text(text, "Checklist item")
    with conn:
        item = load_item(conn, item_id)
        conn.execute(
            "UPDATE checklist_items SET done = ?, text = ? WHERE id = ?",
            (
                item["done"] if done is None else done,
                item["text"] if text is None else text,
                item_id,
            ),
        )
        db.bump_version(conn, item["board_id"])
    return checklist_body(request, conn, item["card_id"])


@router.delete("/checklist/{item_id}", response_class=HTMLResponse)
def delete_checklist_item(
    item_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)
):
    """Delete a checklist item. As with cards, the gap left in positions is fine."""
    with conn:
        item = load_item(conn, item_id)
        conn.execute("DELETE FROM checklist_items WHERE id = ?", (item_id,))
        db.bump_version(conn, item["board_id"])
    return checklist_body(request, conn, item["card_id"])
