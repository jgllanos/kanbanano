"""The login check that guards every route.

It's installed as an app-wide dependency in main.py, so a new route is protected
by default and has to opt out by joining LOGIN_EXEMPT.
"""

from fastapi import HTTPException, Request

# Paths that don't need a login. Static files are always public: they're a
# mount, and app-wide dependencies don't apply to mounts.
LOGIN_EXEMPT = {"/login", "/logout"}


def require_login(request: Request) -> None:
    """App-wide dependency that rejects requests without a logged-in session.

    It applies to every route, so new routes are protected unless they're added
    to LOGIN_EXEMPT.
    """
    if request.url.path in LOGIN_EXEMPT or request.session.get("logged_in"):
        return
    if "HX-Request" in request.headers:
        # htmx would follow a normal redirect inside the request and swap the
        # login page into the target. HX-Redirect navigates the whole page.
        raise HTTPException(
            status_code=401,
            detail="Your session has expired. Log in again.",
            headers={"HX-Redirect": "/login"},
        )
    if request.method == "GET":
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    raise HTTPException(status_code=401, detail="Your session has expired. Log in again.")
