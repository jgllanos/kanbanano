"""Card descriptions rendered as Markdown.

This is the one place the tests assert on markup, because the markup *is* the
behaviour: `app.markdown.render` is a function with an output, not a template
whose shape is free to change. The sanitization cases are the properties the
app relies on instead of a separate sanitizer pass (see app/markdown.py) — if
one of them ever fails, descriptions need nh3 before anything else changes.
"""

import pytest

from app.markdown import render

# Descriptions that would be an injection if they reached the page as live
# HTML, each paired with the markup that must not appear in the output. The
# pair is the tag or attribute form, not the bare word: the escaped text is
# expected to still read "onclick" or "data:text/html", just not as HTML.
DANGEROUS = [
    ("<script>alert(1)</script>", "<script"),
    ('<img src=x onerror="alert(1)">', "<img"),
    ('<a href="#" onclick="alert(1)">t</a>', 'onclick="'),
    ("<div>t</div>", "<div"),
    ("[t](javascript:alert(1))", 'href="javascript:'),
    ("[t](java&#115;cript:alert(1))", 'href="javascript:'),
    ("[t](vbscript:alert(1))", 'href="vbscript:'),
    ("[t](data:text/html;base64,PHNjcmlwdD4=)", 'href="data:'),
    # A code fence's language goes into a class attribute.
    ('```"><script>alert(1)</script>\nx\n```', "<script"),
]


@pytest.mark.parametrize(("source", "forbidden"), DANGEROUS)
def test_dangerous_markup_never_reaches_the_page(source, forbidden):
    assert forbidden not in render(source)


def test_raw_html_is_shown_as_text():
    """Escaped, not dropped: someone writing about HTML sees what they typed."""
    assert render("use <br> here") == "<p>use &lt;br&gt; here</p>\n"


def test_empty_description_renders_nothing():
    assert render("") == ""


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("**bold**", "<p><strong>bold</strong></p>\n"),
        ("*it*", "<p><em>it</em></p>\n"),
        ("~~gone~~", "<p><s>gone</s></p>\n"),
        ("`code`", "<p><code>code</code></p>\n"),
        ("# Title", "<h1>Title</h1>\n"),
        ("- a\n- b", "<ul>\n<li>a</li>\n<li>b</li>\n</ul>\n"),
        ("> quoted", "<blockquote>\n<p>quoted</p>\n</blockquote>\n"),
        # A single newline breaks the line, matching what the textarea showed.
        ("first\nsecond", "<p>first<br>\nsecond</p>\n"),
    ],
)
def test_markdown_is_rendered(source, expected):
    assert render(source) == expected


def test_tables_are_rendered():
    html = render("| a | b |\n| --- | --- |\n| 1 | 2 |")
    assert "<table>" in html
    assert "<th>a</th>" in html
    assert "<td>1</td>" in html


def test_links_open_in_a_new_tab():
    """Following a link shouldn't navigate the board away."""
    assert render("[t](https://nas.test/x)") == (
        '<p><a href="https://nas.test/x" target="_blank" rel="noopener noreferrer">t</a></p>\n'
    )


def test_bare_urls_are_left_alone():
    """No linkify, so a URL is only a link inside <> or []() (see app/markdown.py)."""
    assert render("see http://nas.test/x") == "<p>see http://nas.test/x</p>\n"
    assert "<a" in render("<http://nas.test/x>")


def test_images_fall_back_to_a_link():
    """Image syntax isn't rendered as an <img>: attaching images is its own
    feature, and a remote one wouldn't load on a LAN with no internet."""
    html = render("![shot](http://nas.test/x.png)")
    assert "<img" not in html
    assert ">shot</a>" in html


# ---- Through the app ------------------------------------------------------


def test_description_is_stored_as_typed(client, conn, board):
    """The Markdown is the stored value; rendering happens on the way out, so an
    edit reopens with the source rather than the HTML."""
    card_id = board.cards[0]
    source = "## Steps\n\n- turn **off** the water\n- unscrew"

    response = client.patch(
        f"/cards/{card_id}",
        data={"title": "Tap", "description": source, "updated_at": updated_at(conn, card_id)},
    )

    assert response.status_code == 200
    (stored,) = conn.execute("SELECT description FROM cards WHERE id = ?", (card_id,)).fetchone()
    assert stored == source


def test_modal_shows_the_rendered_description(client, conn, board):
    card_id = board.cards[0]
    client.patch(
        f"/cards/{card_id}",
        data={
            "title": "Tap",
            "description": "**off** the water",
            "updated_at": updated_at(conn, card_id),
        },
    )

    body = client.get(f"/cards/{card_id}").text

    assert "<strong>off</strong>" in body


def test_saving_a_title_leaves_the_description_alone(client, conn, board):
    """The title and the description are separate forms, so each save carries
    one field. A title saved on blur must not take a description that's still
    being edited — or, worse, an empty one — with it."""
    card_id = board.cards[0]
    client.patch(
        f"/cards/{card_id}",
        data={"description": "**off** the water", "updated_at": updated_at(conn, card_id)},
    )

    response = client.patch(
        f"/cards/{card_id}", data={"title": "New tap", "updated_at": updated_at(conn, card_id)}
    )

    assert response.status_code == 200
    title, description = conn.execute(
        "SELECT title, description FROM cards WHERE id = ?", (card_id,)
    ).fetchone()
    assert (title, description) == ("New tap", "**off** the water")


def test_saving_a_description_leaves_the_title_alone(client, conn, board):
    card_id = board.cards[0]

    response = client.patch(
        f"/cards/{card_id}", data={"description": "detail", "updated_at": updated_at(conn, card_id)}
    )

    assert response.status_code == 200
    title, description = conn.execute(
        "SELECT title, description FROM cards WHERE id = ?", (card_id,)
    ).fetchone()
    assert (title, description) == ("Tap", "detail")


def test_a_save_with_no_fields_is_rejected(client, conn, board):
    """Nothing to write, but it would still move updated_at and turn everyone
    else's open modal into a conflict."""
    card_id = board.cards[0]

    response = client.patch(f"/cards/{card_id}", data={"updated_at": updated_at(conn, card_id)})

    assert response.status_code == 400


def test_saving_returns_the_rendered_description(client, conn, board):
    """The save's response carries the new HTML, so the editor can close onto it
    instead of the version the modal was opened with."""
    card_id = board.cards[0]

    response = client.patch(
        f"/cards/{card_id}",
        data={
            "title": "Tap",
            "description": "now **done**",
            "updated_at": updated_at(conn, card_id),
        },
    )

    assert 'hx-swap-oob="innerHTML:#card-description-view"' in response.text
    assert "<strong>done</strong>" in response.text


def updated_at(conn, card_id) -> str:
    """A card's concurrency token, as the modal sends it back."""
    (token,) = conn.execute("SELECT updated_at FROM cards WHERE id = ?", (card_id,)).fetchone()
    return token
