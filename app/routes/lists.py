"""A board's lists: creating, renaming, archiving, restoring, deleting, ordering."""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app import db
from app.queries import (
    NOW,
    archive,
    clean_text,
    id_list,
    load_board,
    load_list,
    require_archived,
    rewrite_positions,
)
from app.routes.boards import archive_response
from app.templating import templates

router = APIRouter()


@router.post("/lists", response_class=HTMLResponse)
def create_list(
    request: Request,
    board_id: Annotated[int, Form()],
    title: Annotated[str, Form()],
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Add a list to the end of a board. Returns the new list's HTML."""
    title = clean_text(title, "List title")
    with conn:
        load_board(conn, board_id)
        list_id = conn.execute(
            """
            INSERT INTO lists (board_id, title, position)
            VALUES (?, ?, (SELECT COALESCE(MAX(position) + 1, 0) FROM lists WHERE board_id = ?))
            """,
            (board_id, title, board_id),
        ).lastrowid
        db.bump_version(conn, board_id)

    lst = dict(load_list(conn, list_id), cards=[])
    return templates.TemplateResponse(request, "_list.html", {"list": lst})


@router.patch("/lists/{list_id}", response_class=HTMLResponse)
def update_list(
    list_id: int,
    request: Request,
    title: Annotated[str, Form()],
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Rename a list. Returns the re-rendered list header."""
    title = clean_text(title, "List title")
    with conn:
        lst = load_list(conn, list_id)
        conn.execute("UPDATE lists SET title = ? WHERE id = ?", (title, list_id))
        db.bump_version(conn, lst["board_id"])
    return templates.TemplateResponse(
        request, "_list_header.html", {"list": load_list(conn, list_id)}
    )


@router.delete("/lists/{list_id}", response_class=HTMLResponse)
def delete_list(list_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    """Delete an archived list and its cards for good, from the archive dialog."""
    with conn:
        lst = load_list(conn, list_id)
        require_archived(lst, "list")
        conn.execute("DELETE FROM lists WHERE id = ?", (list_id,))
        db.bump_version(conn, lst["board_id"])
    return archive_response(request, conn, lst["board_id"])


@router.post("/lists/{list_id}/archive", response_class=HTMLResponse)
def archive_list(list_id: int, conn: sqlite3.Connection = Depends(db.get_db)):
    """Take a list off the board.

    Its cards aren't archived with it. The board doesn't render an archived
    list, so they go off the board with it and come back when it does.
    """
    with conn:
        lst = load_list(conn, list_id)
        archive(conn, "lists", list_id)
        db.bump_version(conn, lst["board_id"])
    return HTMLResponse("")


@router.post("/lists/{list_id}/restore", response_class=HTMLResponse)
def restore_list(list_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    """Put an archived list back on the board, with the cards it still holds."""
    with conn:
        lst = load_list(conn, list_id)
        conn.execute("UPDATE lists SET archived_at = NULL WHERE id = ?", (list_id,))
        db.bump_version(conn, lst["board_id"])
    return archive_response(request, conn, lst["board_id"])


@router.post("/lists/{list_id}/archive-cards", response_class=HTMLResponse)
def archive_list_cards(
    list_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)
):
    """Archive every card in a list, keeping the list itself.

    Returns the list's empty card container, so the board shows the result
    without waiting for a poll.
    """
    with conn:
        lst = load_list(conn, list_id)
        conn.execute(
            f"UPDATE cards SET archived_at = {NOW} WHERE list_id = ? AND archived_at IS NULL",
            (list_id,),
        )
        db.bump_version(conn, lst["board_id"])
    return templates.TemplateResponse(request, "_cards.html", {"list": dict(lst, cards=[])})


@router.post("/lists/reorder", status_code=204)
def reorder_lists(
    board_id: Annotated[int, Form()],
    list_ids: Annotated[list[int], Form()],
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Save list order after a drag. Takes the board's full list order."""
    with conn:
        load_board(conn, board_id)
        on_board = {
            row["id"]
            for row in conn.execute(
                """
                SELECT id FROM lists
                WHERE board_id = ? AND id IN (SELECT value FROM json_each(?))
                """,
                (board_id, id_list(list_ids)),
            )
        }
        known = [id_ for id_ in dict.fromkeys(list_ids) if id_ in on_board]
        rewrite_positions(conn, "lists", "board_id", board_id, known)
        db.bump_version(conn, board_id)
