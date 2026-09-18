"""Cards: creating, the modal and its title and description, ordering, archiving.

Labels and checklists hang off a card but have routes of their own, in labels.py
and checklist.py.
"""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import db
from app.queries import (
    NOW,
    archive,
    board_labels,
    clean_text,
    id_list,
    load_card,
    load_checklist,
    load_list,
    require_archived,
    rewrite_positions,
)
from app.routes.boards import archive_response
from app.templating import templates

router = APIRouter()


@router.post("/cards", response_class=HTMLResponse)
def create_card(
    request: Request,
    list_id: Annotated[int, Form()],
    title: Annotated[str, Form()],
    label_ids: Annotated[list[int] | None, Form()] = None,
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Add a card to the bottom of a list. Returns the new card's HTML.

    `label_ids` are labels and people to assign right away: the ones the board
    filter is showing (see the composer in _list.html). Ids that aren't on the
    list's board, for example deleted since the page loaded, are skipped.
    """
    title = clean_text(title, "Card title")
    with conn:
        lst = load_list(conn, list_id)
        card_id = conn.execute(
            """
            INSERT INTO cards (list_id, title, position)
            VALUES (?, ?, (SELECT COALESCE(MAX(position) + 1, 0) FROM cards WHERE list_id = ?))
            """,
            (list_id, title, list_id),
        ).lastrowid
        conn.execute(
            """
            INSERT OR IGNORE INTO card_labels (card_id, label_id)
            SELECT ?, id FROM labels
            WHERE board_id = ? AND id IN (SELECT value FROM json_each(?))
            """,
            (card_id, lst["board_id"], id_list(label_ids or [])),
        )
        db.bump_version(conn, lst["board_id"])

    return templates.TemplateResponse(request, "_card.html", {"card": load_card(conn, card_id)})


@router.post("/cards/reorder", status_code=204)
def reorder_cards(
    list_id: Annotated[int, Form()],
    card_ids: Annotated[list[int], Form()],
    from_list_id: Annotated[int | None, Form()] = None,
    from_card_ids: Annotated[list[int] | None, Form()] = None,
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Save card order after a drag.

    Takes the full card order of the list the card was dropped in and, for a
    move between lists, the full order of the list it left (possibly empty).
    """
    from_card_ids = from_card_ids or []
    orders = {list_id: card_ids}
    if from_list_id is not None and from_list_id != list_id:
        orders[from_list_id] = from_card_ids

    with conn:
        lists = conn.execute(
            "SELECT id, board_id FROM lists WHERE id IN (SELECT value FROM json_each(?))",
            (id_list(orders),),
        ).fetchall()
        if len(lists) < len(orders):
            raise HTTPException(status_code=404, detail="This list was deleted")
        board_ids = {lst["board_id"] for lst in lists}
        if len(board_ids) > 1:
            raise HTTPException(status_code=400, detail="Can't move cards between boards")
        board_id = board_ids.pop()

        # Drop ids for cards deleted since the page loaded, or from other boards.
        on_board = {
            row["id"]
            for row in conn.execute(
                """
                SELECT cards.id FROM cards JOIN lists ON lists.id = cards.list_id
                WHERE lists.board_id = ? AND cards.id IN (SELECT value FROM json_each(?))
                """,
                (board_id, id_list(card_ids + from_card_ids)),
            )
        }
        in_request = set(card_ids) | set(from_card_ids)
        for target_list_id, ids in orders.items():
            known = [id_ for id_ in dict.fromkeys(ids) if id_ in on_board]
            rewrite_positions(conn, "cards", "list_id", target_list_id, known, in_request)
        db.bump_version(conn, board_id)


@router.get("/cards/{card_id}", response_class=HTMLResponse)
def card_detail(card_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    """The card modal: a <dialog> that opens itself once swapped into #modal-root."""
    card = load_card(conn, card_id)
    return templates.TemplateResponse(
        request,
        "_card_modal.html",
        {
            "card": card,
            "options": board_labels(conn, card["board_id"]),
            "items": load_checklist(conn, card_id),
        },
    )


@router.get("/cards/{card_id}/status")
def card_status(card_id: int, conn: sqlite3.Connection = Depends(db.get_db)):
    """Whether a card is still on its board, only archived, or gone for good.

    The focus timer asks this when the card it's tracking isn't on the page.
    Archiving takes a card off the board, and so does archiving its list or its
    board, all of which look exactly like a deletion from the client's side. A
    card that's only archived stays in the dock, because restoring it brings it
    back (see syncFocusWithPage in timer.js).
    """
    row = conn.execute(
        """
        SELECT (cards.archived_at IS NOT NULL
                OR lists.archived_at IS NOT NULL
                OR boards.archived_at IS NOT NULL) AS archived
        FROM cards
        JOIN lists ON lists.id = cards.list_id
        JOIN boards ON boards.id = lists.board_id
        WHERE cards.id = ?
        """,
        (card_id,),
    ).fetchone()
    if row is None:
        return {"state": "gone"}
    return {"state": "archived" if row["archived"] else "board"}


@router.patch("/cards/{card_id}", response_class=HTMLResponse)
def update_card(
    card_id: int,
    request: Request,
    updated_at: Annotated[str, Form()],
    title: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Save the modal's title, its description, or both.

    The two are separate forms, so a request normally carries one of them: the
    title saves itself when it loses focus, while the description is saved by
    hand, and neither should drag the other's half-finished text along with it.
    A field that isn't sent is left as it is.

    `updated_at` is the value the modal was loaded with, shared by both forms
    (see #card-updated-at). If the card has been saved since, this save is
    rejected with a 409 carrying the stored description and the current token,
    so the description editor can offer to overwrite that version, take it, or
    merge the two (see descriptionEditor in app.js).
    """
    # Column names come from here, not from the request.
    changes = {}
    if title is not None:
        changes["title"] = clean_text(title, "Card title")
    if description is not None:
        changes["description"] = "\n".join(description.splitlines()).strip()  # browsers send \r\n
    if not changes:
        raise HTTPException(status_code=400, detail="That save was empty")

    with conn:
        card = load_card(conn, card_id)
        stale = card["updated_at"] != updated_at
        if not stale:
            assignments = ", ".join(f"{column} = ?" for column in changes)
            conn.execute(
                f"UPDATE cards SET {assignments}, updated_at = {NOW} WHERE id = ?",
                (*changes.values(), card_id),
            )
            db.bump_version(conn, card["board_id"])
    if stale:
        # Not an HTTPException: the editor needs the version it's up against,
        # not just a message. Returned after the transaction so the rejected
        # save leaves nothing behind.
        return JSONResponse(
            status_code=409,
            content={
                "detail": "Someone else changed this card while you were editing it.",
                "description": card["description"],
                "updated_at": card["updated_at"],
            },
        )
    return templates.TemplateResponse(
        request, "_card_saved.html", {"card": load_card(conn, card_id)}
    )


@router.delete("/cards/{card_id}", response_class=HTMLResponse)
def delete_card(card_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    """Delete an archived card for good. Returns the archive dialog's contents."""
    with conn:
        card = load_card(conn, card_id)
        require_archived(card, "card")
        conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
        db.bump_version(conn, card["board_id"])
    return archive_response(request, conn, card["board_id"])


@router.post("/cards/{card_id}/archive", response_class=HTMLResponse)
def archive_card(card_id: int, conn: sqlite3.Connection = Depends(db.get_db)):
    """Take a card off the board, keeping everything on it.

    Returns an empty 200 because htmx skips swapping on a 204, and the swap is
    what removes the card from the page. The gap left in the list's positions
    doesn't affect the order, and the next drag in that list renumbers it.
    """
    with conn:
        card = load_card(conn, card_id)
        archive(conn, "cards", card_id)
        db.bump_version(conn, card["board_id"])
    return HTMLResponse("")


@router.post("/cards/{card_id}/restore", response_class=HTMLResponse)
def restore_card(card_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    """Put an archived card back on its list.

    Restores the list too if that's archived, so the card comes back somewhere
    visible rather than into a column nobody can see.
    """
    with conn:
        card = load_card(conn, card_id)
        conn.execute("UPDATE cards SET archived_at = NULL WHERE id = ?", (card_id,))
        conn.execute("UPDATE lists SET archived_at = NULL WHERE id = ?", (card["list_id"],))
        db.bump_version(conn, card["board_id"])
    return archive_response(request, conn, card["board_id"])
