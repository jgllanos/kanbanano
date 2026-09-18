"""The index, a board itself, its background, its archive, and the poll.

archive_response lives here because restoring or deleting anything sends the
board's lists back with it, and lists.py and cards.py both need that.
"""

import secrets
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from app import db
from app.queries import (
    archive,
    board_labels,
    clean_text,
    index_tiles,
    load_archive,
    load_board,
    load_lists,
    require_archived,
)
from app.templating import templates
from app.theme import BACKGROUND_TYPE, UPLOAD_LIMIT, clean_background, clean_background_image

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def board_index(request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    return templates.TemplateResponse(request, "index.html", index_tiles(conn))


@router.post("/boards")
def create_board(title: Annotated[str, Form()], conn: sqlite3.Connection = Depends(db.get_db)):
    """Create an empty board and redirect to it.

    Uses HX-Redirect instead of a 303 so errors show up as a flash message, the
    same as everywhere else.
    """
    title = clean_text(title, "Board title")
    with conn:
        board_id = conn.execute("INSERT INTO boards (title) VALUES (?)", (title,)).lastrowid
    return Response(status_code=204, headers={"HX-Redirect": f"/boards/{board_id}"})


@router.get("/boards/{board_id}", response_class=HTMLResponse)
def board_view(board_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    return templates.TemplateResponse(
        request,
        "board.html",
        {
            "board": load_board(conn, board_id),
            "lists": load_lists(conn, board_id),
            "options": board_labels(conn, board_id),
        },
    )


@router.patch("/boards/{board_id}", response_class=HTMLResponse)
def update_board(
    board_id: int,
    request: Request,
    title: Annotated[str | None, Form()] = None,
    background: Annotated[str | None, Form()] = None,
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Rename a board or set its background. Send whichever one changed.

    A rename returns the header, so the title field's value attribute (which
    Escape reverts to) matches the saved title. A background change returns only
    the settings menu, so the menu stays open.
    """
    if title is None and background is None:
        # Writing the row back unchanged would still bump the version, and send
        # every other client to refetch the whole board for nothing.
        raise HTTPException(status_code=400, detail="That save was empty")
    if title is not None:
        title = clean_text(title, "Board title")
    if background is not None:
        background = clean_background(background)

    with conn:
        board = load_board(conn, board_id)
        conn.execute(
            "UPDATE boards SET title = ?, background = ? WHERE id = ?",
            (
                board["title"] if title is None else title,
                board["background"] if background is None else background,
                board_id,
            ),
        )
        if background is not None:
            # One background per board, so picking a color drops the photo.
            conn.execute("DELETE FROM board_images WHERE board_id = ?", (board_id,))
        db.bump_version(conn, board_id)

    board = load_board(conn, board_id)
    if background is not None:
        return templates.TemplateResponse(request, "_board_menu_saved.html", {"board": board})
    return templates.TemplateResponse(
        request,
        "_board_header.html",
        {"board": board, "options": board_labels(conn, board_id)},
    )


def archive_response(request: Request, conn: sqlite3.Connection, board_id: int) -> Response:
    """The archive dialog's contents, plus the board's lists out-of-band.

    The board's lists are sent with the response because a client's own write
    never comes back to it through a poll (see adoptVersion in app.js).
    """
    return templates.TemplateResponse(
        request,
        "_archive_refresh.html",
        {
            "board": load_board(conn, board_id),
            "archive": load_archive(conn, board_id),
            "lists": load_lists(conn, board_id),
        },
    )


@router.get("/boards/{board_id}/archive", response_class=HTMLResponse)
def board_archive(board_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    """The archive dialog, loaded when it's opened rather than with the board."""
    return templates.TemplateResponse(
        request,
        "_archive_modal.html",
        {"board": load_board(conn, board_id), "archive": load_archive(conn, board_id)},
    )


@router.post("/boards/{board_id}/archive")
def archive_board(board_id: int, conn: sqlite3.Connection = Depends(db.get_db)):
    """Take a board off the index, keeping everything on it."""
    with conn:
        load_board(conn, board_id)
        archive(conn, "boards", board_id)
        db.bump_version(conn, board_id)
    return Response(status_code=204, headers={"HX-Redirect": "/"})


@router.post("/boards/{board_id}/restore", response_class=HTMLResponse)
def restore_board(board_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    """Put an archived board back on the index. Returns the index's tiles."""
    with conn:
        load_board(conn, board_id)
        conn.execute("UPDATE boards SET archived_at = NULL WHERE id = ?", (board_id,))
        db.bump_version(conn, board_id)
    return templates.TemplateResponse(request, "_board_tiles.html", index_tiles(conn))


@router.post("/boards/{board_id}/background", response_class=HTMLResponse)
def upload_background(
    request: Request,
    board_id: int,
    image: Annotated[UploadFile, File()],
    conn: sqlite3.Connection = Depends(db.get_db),
):
    """Set a board's background to an uploaded photo.

    Returns the settings menu, like picking a color does, so the menu stays
    open. The board's background becomes 'image:<token>'. The token is also in
    the image's URL, so a replacement photo gets a URL nothing has cached.
    """
    data = image.file.read(UPLOAD_LIMIT + 1)
    if len(data) > UPLOAD_LIMIT:
        raise HTTPException(status_code=400, detail="That image is too big (12MB at most)")
    jpeg = clean_background_image(data)
    token = secrets.token_hex(8)

    with conn:
        load_board(conn, board_id)
        conn.execute(
            """
            INSERT INTO board_images (board_id, token, content_type, data) VALUES (?, ?, ?, ?)
            ON CONFLICT (board_id) DO UPDATE
                SET token = excluded.token, content_type = excluded.content_type,
                    data = excluded.data
            """,
            (board_id, token, BACKGROUND_TYPE, jpeg),
        )
        conn.execute("UPDATE boards SET background = ? WHERE id = ?", (f"image:{token}", board_id))
        db.bump_version(conn, board_id)

    return templates.TemplateResponse(
        request, "_board_menu_saved.html", {"board": load_board(conn, board_id)}
    )


@router.get("/boards/{board_id}/background/{token}")
def board_background(board_id: int, token: str, conn: sqlite3.Connection = Depends(db.get_db)):
    """A board's uploaded background.

    A new upload gets a new token and a new URL, so this response can be cached
    indefinitely. This is a normal route, so require_login applies to it; files
    under /static are a mount, and mounts skip app-wide dependencies.
    """
    row = conn.execute(
        "SELECT content_type, data FROM board_images WHERE board_id = ? AND token = ?",
        (board_id, token),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="This image is gone")
    return Response(
        row["data"],
        media_type=row["content_type"],
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            # Don't let a browser guess the type from the bytes.
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/boards/{board_id}", response_class=HTMLResponse)
def delete_board(board_id: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)):
    """Delete an archived board and everything on it. Returns the index's tiles.

    There's no version to bump once the row is gone. Other clients' polls get a
    286 instead.
    """
    with conn:
        require_archived(load_board(conn, board_id), "board")
        conn.execute("DELETE FROM boards WHERE id = ?", (board_id,))
    return templates.TemplateResponse(request, "_board_tiles.html", index_tiles(conn))


@router.get("/boards/{board_id}/poll", response_class=HTMLResponse)
def poll_board(
    board_id: int, v: int, request: Request, conn: sqlite3.Connection = Depends(db.get_db)
):
    """204 if version `v` is current, otherwise the re-rendered lists container.

    The version is read before the lists. If a write lands in between, the
    content is newer than the version sent with it, so the next poll fetches
    again and no change is missed.
    """
    board = conn.execute("SELECT * FROM boards WHERE id = ?", (board_id,)).fetchone()
    if board is None or board["archived_at"] is not None:
        # 286 tells htmx to stop polling; the message replaces the lists. An
        # archived board still renders, so this is how someone else looking at
        # it finds out that it has left the index.
        gone = "deleted" if board is None else "archived"
        return HTMLResponse(
            f'<div id="lists-container" class="lists board-gone">This board was {gone}.</div>',
            status_code=286,
        )
    if board["version"] == v:
        return Response(status_code=204)
    return templates.TemplateResponse(
        request,
        "_board_refresh.html",
        {
            "board": board,
            "lists": load_lists(conn, board_id),
            "options": board_labels(conn, board_id),
        },
    )
