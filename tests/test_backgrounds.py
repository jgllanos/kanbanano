"""Uploaded board backgrounds.

Uploads are decoded and re-encoded (see main.clean_background_image), so most of
these check the stored image rather than the bytes that were sent.
"""

import io

from PIL import Image

from app.main import UPLOAD_LIMIT
from conftest import delete_for_good, version


def photo(size=(1200, 800), mode="RGB", format="JPEG", **save) -> bytes:
    """An image file's bytes, as a browser would send them."""
    data = io.BytesIO()
    Image.new(mode, size, "#336699").save(data, format, **save)
    return data.getvalue()


def upload(client, board_id, data=None, filename="photo.jpg", content_type="image/jpeg"):
    return client.post(
        f"/boards/{board_id}/background",
        files={"image": (filename, photo() if data is None else data, content_type)},
    )


def stored(conn):
    """The one saved image, as (token, content_type, PIL image)."""
    row = conn.execute("SELECT * FROM board_images").fetchone()
    return row["token"], row["content_type"], Image.open(io.BytesIO(row["data"]))


def background(conn) -> str:
    return conn.execute("SELECT background FROM boards").fetchone()[0]


def test_uploading_a_photo_sets_the_background(client, conn, board):
    response = upload(client, board.id)

    token, content_type, _ = stored(conn)
    assert background(conn) == f"image:{token}"
    assert content_type == "image/jpeg"
    assert version(conn, board.id) == 1
    # Only the menu is returned, so it stays open, as with a color.
    assert 'id="board-menu-panel"' in response.text


def test_the_photo_comes_back_from_its_url(client, conn, board):
    upload(client, board.id)
    token, _, _ = stored(conn)

    response = client.get(f"/boards/{board.id}/background/{token}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert "immutable" in response.headers["cache-control"]
    assert Image.open(io.BytesIO(response.content)).format == "JPEG"


def test_another_boards_token_is_not_accepted(client, conn, board):
    upload(client, board.id)
    token, _, _ = stored(conn)
    other = conn.execute("INSERT INTO boards (title) VALUES ('Other')").lastrowid

    assert client.get(f"/boards/{board.id}/background/{token}x").status_code == 404
    assert client.get(f"/boards/{other}/background/{token}").status_code == 404


def test_the_photo_reaches_the_board_and_the_index(client, conn, board):
    upload(client, board.id)
    token, _, _ = stored(conn)

    page = client.get(f"/boards/{board.id}").text
    assert f"--board-bg: url(/boards/{board.id}/background/{token})" in page
    assert "--board-ink: 255 255 255" in page  # photos always get white text
    assert "has-photo" in client.get("/").text  # the tile on the index


def test_a_big_photo_is_shrunk(client, conn, board):
    upload(client, board.id, photo((3000, 2000)))

    _, _, image = stored(conn)
    assert image.size == (1920, 1280)  # the aspect ratio is kept


def test_a_small_photo_is_left_at_its_size(client, conn, board):
    upload(client, board.id, photo((400, 300)))

    _, _, image = stored(conn)
    assert image.size == (400, 300)


def test_a_rotated_photo_is_turned_the_right_way_up(client, conn, board):
    """Phone photos are stored sideways with an EXIF tag saying which way up."""
    exif = Image.Exif()
    exif[274] = 6  # orientation: rotate a quarter turn
    upload(client, board.id, photo((400, 200), exif=exif.tobytes()))

    _, _, image = stored(conn)
    assert image.size == (200, 400)
    assert dict(image.getexif()) == {}, "EXIF is dropped, including where it was taken"


def test_a_transparent_png_becomes_a_jpeg(client, conn, board):
    upload(client, board.id, photo(mode="RGBA", format="PNG"), "shot.png", "image/png")

    _, content_type, image = stored(conn)
    assert (content_type, image.format, image.mode) == ("image/jpeg", "JPEG", "RGB")


def test_a_file_that_isnt_an_image_is_rejected(client, conn, board):
    """Without this, a .jpg that's really HTML would be served from our own origin."""
    for data in [b"<html><script>alert(1)</script></html>", b"", photo()[:200]]:
        response = upload(client, board.id, data)
        assert response.status_code == 400, data[:20]

    assert conn.execute("SELECT COUNT(*) FROM board_images").fetchone()[0] == 0
    assert background(conn) == "#0079bf"
    assert version(conn, board.id) == 0


def test_an_upload_over_the_limit_is_rejected(client, conn, board):
    response = upload(client, board.id, b"\xff\xd8\xff" + b"\0" * UPLOAD_LIMIT)

    assert response.status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM board_images").fetchone()[0] == 0


def test_uploading_again_replaces_the_photo(client, conn, board):
    upload(client, board.id, photo((400, 300)))
    first, _, _ = stored(conn)

    upload(client, board.id, photo((500, 400)))

    second, _, image = stored(conn)
    assert conn.execute("SELECT COUNT(*) FROM board_images").fetchone()[0] == 1
    assert second != first
    assert image.size == (500, 400)
    # The old URL stops working, so no browser keeps showing the old photo.
    assert client.get(f"/boards/{board.id}/background/{first}").status_code == 404


def test_picking_a_color_drops_the_photo(client, conn, board):
    upload(client, board.id)

    client.patch(f"/boards/{board.id}", data={"background": "#b04632"})

    assert background(conn) == "#b04632"
    assert conn.execute("SELECT COUNT(*) FROM board_images").fetchone()[0] == 0


def test_renaming_the_board_keeps_the_photo(client, conn, board):
    upload(client, board.id)
    token, _, _ = stored(conn)

    client.patch(f"/boards/{board.id}", data={"title": "House"})

    assert background(conn) == f"image:{token}"


def test_deleting_the_board_takes_the_photo_with_it(client, conn, board):
    upload(client, board.id)

    delete_for_good(client, "boards", board.id)

    assert conn.execute("SELECT COUNT(*) FROM board_images").fetchone()[0] == 0
