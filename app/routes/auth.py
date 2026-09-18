"""Logging in and out. These are the only paths in LOGIN_EXEMPT."""

import secrets
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import BOARD_PASSWORD
from app.templating import templates

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("logged_in"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html")


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, password: Annotated[str, Form()] = ""):
    """Check the shared password and start a session.

    compare_digest takes the same time no matter how much of a guess matches.
    """
    if not secrets.compare_digest(password.encode(), BOARD_PASSWORD.encode()):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Wrong password."}, status_code=401
        )
    request.session["logged_in"] = True
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
