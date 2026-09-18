"""The app itself: middleware, the static mount, and the routes.

Everything else lives next door — config.py for settings, security.py for the
login check, queries.py for reading the database, theme.py for how a board
looks, templating.py for the Jinja environment, and routes/ for the endpoints.
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import db
from app.config import ALLOW_HTTP, BASE_DIR, SECRET_KEY, SESSION_MAX_AGE
from app.routes import ROUTERS
from app.security import require_login


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    try:
        db.init_db(conn)
    finally:
        conn.close()
    yield


# The API docs are turned off. FastAPI serves them outside the dependency system,
# so require_login wouldn't protect them.
app = FastAPI(
    lifespan=lifespan,
    dependencies=[Depends(require_login)],
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# The dependency above is on the app, so it covers every route in every one of
# these. test_auth.py walks app.routes to check that.
for router in ROUTERS:
    app.include_router(router)


@app.middleware("http")
async def revalidate_static_files(request: Request, call_next):
    """Make browsers check static files for changes on every load.

    Without a Cache-Control header they may reuse a cached copy for a while
    without asking, so a page can keep running old JavaScript after an update.
    Checking costs a 304 when nothing changed.
    """
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.middleware("http")
async def report_board_version(request: Request, call_next):
    """Send the board version a write produced, in an X-Board-Version header.

    The client uses it to avoid refetching its own change on the next poll (see
    adoptVersion in app.js).

    Only on a success: bump_version records the new version on the connection as
    a side effect, and a transaction that rolled back after bumping would leave
    it holding a version nobody else will ever see.
    """
    response = await call_next(request)
    conn = getattr(request.state, "db", None)
    if response.status_code < 400 and conn is not None and conn.board_version is not None:
        response.headers["X-Board-Version"] = str(conn.board_version)
    return response


# Added last so it runs first, before anything reads the session. The cookie is
# signed but not encrypted, and only holds the logged-in flag. Starlette re-sends
# it on every response, so the year counts from a device's last visit.
#
# same_site="lax" is the only thing stopping another site from posting here as a
# logged-in visitor; there are no CSRF tokens behind it. Browsers keep the cookie
# off a cross-site POST, but still send it when someone follows a link in, which
# is a GET. So no route may change anything on a GET: one could be set off by a
# link or an <img> on any page anywhere, with the cookie attached. Every write
# here is a POST, PATCH or DELETE, and has to stay that way.
# test_the_session_cookie_is_locked_down pins the setting itself.
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    max_age=SESSION_MAX_AGE,
    same_site="lax",
    https_only=not ALLOW_HTTP,
)
