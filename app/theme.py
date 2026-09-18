"""How a board looks: its colors, its uploaded background, and the ink over them.

Everything a board's appearance depends on ends up inside a <style> element or a
style attribute, where HTML escaping doesn't help — `red; } body { display: none }`
has nothing to escape. So every value that gets there is validated to an exact
shape here, on the way in, and the templates can use it without further thought.
"""

import io
import re
import sqlite3

from fastapi import HTTPException
from PIL import Image, ImageOps

from app import db

HEX_COLOR = re.compile(r"#[0-9a-f]{6}", re.IGNORECASE)


def check_color(color: str) -> None:
    # Label colors go into style attributes, so this also keeps arbitrary CSS
    # out of the page.
    if color not in db.LABEL_COLORS.values():
        raise HTTPException(status_code=400, detail="Pick a color from the palette")


def clean_background(color: str) -> str:
    """Validate a board background: any #rrggbb color, returned lower-cased.

    The value is written into a <style> element, where HTML escaping doesn't
    help (`red; } body { display: none }` has nothing to escape), so only exact
    hex colors are accepted.
    """
    if not HEX_COLOR.fullmatch(color):
        raise HTTPException(status_code=400, detail="Pick a color")
    return color.lower()


# Uploaded backgrounds. Every upload is re-encoded, so the stored bytes are
# always a JPEG this app wrote.
UPLOAD_LIMIT = 12 * 1024 * 1024  # the most that's read from an upload
BACKGROUND_SIZE = (1920, 1920)  # a board background is never shown larger
BACKGROUND_TYPE = "image/jpeg"


def clean_background_image(data: bytes) -> bytes:
    """Turn an uploaded file into a JPEG to use as a board background.

    The upload is decoded and re-encoded, so only an image this app wrote is
    ever served back. An HTML file named .jpg fails to decode and is rejected.
    Re-encoding drops EXIF, including where a photo was taken. Shrinking keeps
    a phone photo from being sent at full size to every device that opens the
    board.
    """
    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))  # apply the rotation flag
        image.thumbnail(BACKGROUND_SIZE)  # never enlarges, and keeps the aspect ratio
        # JPEG has no transparency. Converting straight to RGB would put
        # transparent areas on black, so they're composited onto white below.
        image = image.convert("RGBA")
    except Exception:  # Pillow raises several different errors for a file it can't read
        raise HTTPException(status_code=400, detail="That doesn't look like an image")

    flat = Image.new("RGB", image.size, "white")
    flat.paste(image, mask=image.getchannel("A"))
    out = io.BytesIO()
    flat.save(out, "JPEG", quality=82, optimize=True, progressive=True)
    return out.getvalue()


def relative_luminance(color: str) -> float:
    """WCAG relative luminance of a #rrggbb color: 0 for black, 1 for white."""

    def linear(channel: int) -> float:
        c = channel / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)


def needs_dark_ink(background: str) -> bool:
    """Whether text on this background should be dark instead of white.

    Text stays white unless its contrast drops below 3:1, the WCAG minimum for
    large text and controls. Choosing whichever color has more contrast would
    switch several presets to dark text over very small differences.

    A photo has no single brightness to measure, so uploaded backgrounds always
    get white text. The header and the lists panel are shaded over the photo,
    and everything else on the board has an opaque background of its own.
    """
    if not HEX_COLOR.fullmatch(background):
        return False
    contrast_with_white = 1.05 / (relative_luminance(background) + 0.05)
    return contrast_with_white < 3


def image_token(background: str) -> str | None:
    """The token in an 'image:<token>' background, or None for a color."""
    prefix, _, token = background.partition(":")
    return token if prefix == "image" else None


def background_css(board: sqlite3.Row) -> str:
    """The CSS `background` value for a board: its color, or its uploaded image.

    This goes into a <style> element, like clean_background's color, so it must
    not be attacker-controlled. The board id is an integer and the token is hex
    from secrets, so nothing else can reach the URL. The url() is unquoted
    because Jinja escapes quotes inside a style, and CSS allows it.
    """
    token = image_token(board["background"])
    if token is None:
        return board["background"]
    return f"url(/boards/{board['id']}/background/{token}) center / cover no-repeat"
