"""The shared-password login, and what it keeps out."""

import base64
import json

import pytest
from fastapi.routing import APIRoute
from itsdangerous import TimestampSigner

from app.main import LOGIN_EXEMPT, SESSION_MAX_AGE, app
from conftest import PASSWORD, make_client, version


def protected_requests():
    """(method, url) for every route that needs a login, with ids filled in.

    Built from the app's route table, so new routes are checked automatically.
    """
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.path in LOGIN_EXEMPT:
            continue
        url = route.path
        for param in route.param_convertors:
            url = url.replace("{" + param + "}", "1")
        for method in sorted(route.methods - {"HEAD"}):
            yield method, url


@pytest.mark.parametrize("method, url", list(protected_requests()))
def test_every_route_turns_away_a_visitor_without_a_session(anon, conn, board, method, url):
    page = anon.request(method, url, follow_redirects=False)
    htmx = anon.request(method, url, headers={"HX-Request": "true"}, follow_redirects=False)

    if method == "GET":
        assert (page.status_code, page.headers["location"]) == (303, "/login")
    else:
        assert page.status_code == 401
    # htmx requests get HX-Redirect instead of a redirect.
    assert (htmx.status_code, htmx.headers["hx-redirect"]) == (401, "/login")
    assert version(conn, board.id) == 0  # and nothing was written


def test_protected_routes_are_found():
    """Checks the route list used above isn't empty or missing routes."""
    urls = {url for _, url in protected_requests()}
    assert {"/", "/boards/1", "/boards/1/poll", "/cards/reorder", "/checklist/1"} <= urls


def test_static_files_need_no_login(anon):
    assert anon.get("/static/app.css").status_code == 200


def test_the_login_page_needs_no_login(anon):
    assert anon.get("/login").status_code == 200


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_docs_are_not_served(anon, path):
    """FastAPI serves these outside the dependency system, so login wouldn't protect them."""
    assert anon.get(path, follow_redirects=False).status_code in (303, 404)
    assert "openapi" not in anon.get(path).text.lower()


def test_the_wrong_password_is_refused(anon):
    response = anon.post("/login", data={"password": "incorrect horse"}, follow_redirects=False)

    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert anon.get("/", follow_redirects=False).status_code == 303


def test_an_empty_password_is_refused(anon):
    assert anon.post("/login", data={"password": ""}, follow_redirects=False).status_code == 401


def test_the_right_password_starts_a_session(anon):
    response = anon.post("/login", data={"password": PASSWORD}, follow_redirects=False)

    assert (response.status_code, response.headers["location"]) == (303, "/")
    assert anon.get("/", follow_redirects=False).status_code == 200


def test_the_session_cookie_is_locked_down(anon):
    response = anon.post("/login", data={"password": PASSWORD}, follow_redirects=False)

    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=lax" in cookie
    assert f"max-age={SESSION_MAX_AGE}" in cookie


def test_the_login_page_sends_a_logged_in_visitor_on(client):
    response = client.get("/login", follow_redirects=False)

    assert (response.status_code, response.headers["location"]) == (303, "/")


def test_log_out(client):
    response = client.post("/logout", follow_redirects=False)

    assert (response.status_code, response.headers["location"]) == (303, "/login")
    assert client.get("/", follow_redirects=False).status_code == 303


def session_cookie(secret: str, session: dict) -> str:
    """A session cookie as Starlette's SessionMiddleware would sign it."""
    payload = base64.b64encode(json.dumps(session).encode())
    return TimestampSigner(secret).sign(payload).decode()


def test_a_session_signed_with_another_key_is_ignored(conn):
    """Changing SECRET_KEY logs every device out."""
    visitor = make_client()
    visitor.cookies.set("session", session_cookie("some-other-key", {"logged_in": True}))

    assert visitor.get("/", follow_redirects=False).status_code == 303


def test_a_hand_edited_cookie_is_ignored(conn):
    visitor = make_client()
    unsigned = base64.b64encode(json.dumps({"logged_in": True}).encode()).decode()
    visitor.cookies.set("session", unsigned)

    assert visitor.get("/", follow_redirects=False).status_code == 303


def test_the_forged_cookie_helper_is_faithful(conn):
    """Checks session_cookie itself: with the real key, the cookie works."""
    visitor = make_client()
    visitor.cookies.set("session", session_cookie("test-secret-key", {"logged_in": True}))

    assert visitor.get("/", follow_redirects=False).status_code == 200
