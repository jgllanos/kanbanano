"""The Jinja environment, and everything the templates can call.

Every filter and global the templates use is registered here, so a template can
be read against one list rather than hunting through the route modules.
"""

from fastapi.templating import Jinja2Templates

from app import db, markdown, theme
from app.config import BASE_DIR

templates = Jinja2Templates(directory=BASE_DIR / "templates")


def initials(name: str) -> str:
    """'Alex Smith' -> 'AS', 'Sam' -> 'S'."""
    return "".join(word[0] for word in name.split()[:2]).upper()


def filter_options(options: dict[str, list]) -> list[dict]:
    """A board's labels, then its people, as JSON-ready dicts for the filter and
    the card composer (see _filter.html and the filter store in app.js)."""
    return [
        {
            "id": row["id"],
            "kind": row["kind"],
            "name": row["name"],
            "color": row["color"],
            "initials": initials(row["name"]),
        }
        for kind in ("label", "person")
        for row in options[kind]
    ]


templates.env.filters["initials"] = initials
templates.env.filters["filter_options"] = filter_options
templates.env.filters["markdown"] = markdown.render
templates.env.filters["needs_dark_ink"] = theme.needs_dark_ink
templates.env.filters["background_css"] = theme.background_css
templates.env.filters["image_token"] = theme.image_token

templates.env.globals["LABEL_COLORS"] = db.LABEL_COLORS
templates.env.globals["BOARD_COLORS"] = db.BOARD_COLORS
