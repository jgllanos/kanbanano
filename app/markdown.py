"""Card descriptions are Markdown, rendered to HTML on the way out.

Descriptions are stored as the text that was typed. Rendering happens per
request, for one card at a time — only the card modal shows a description, so
this never runs for a whole board.

There's no separate sanitizer pass, because nothing here emits HTML the parser
didn't build itself:

- `html: False` makes markdown-it escape raw HTML in the source instead of
  passing it through, so a `<script>` in a description renders as visible text.
- Link destinations go through markdown-it's own validation, which rejects
  `javascript:`, `vbscript:`, `file:` and `data:` URLs (bar a few image types).
- Everything else that reaches an attribute, such as a code fence's language,
  is HTML-escaped by the renderer.

test_markdown.py pins all three. Enabling `html`, or adding a plugin that
passes raw HTML through, breaks that reasoning and means adding a sanitizer
(nh3) to go with it.
"""

from markdown_it import MarkdownIt
from markupsafe import Markup

_MARKDOWN = (
    MarkdownIt(
        "commonmark",
        {
            "html": False,  # see the module docstring
            "breaks": True,  # a single newline is a line break, as in the textarea
            "xhtmlOut": False,
        },
    )
    # Tables and ~~strikethrough~~ aren't CommonMark, but they're what people
    # expect from Markdown. Bare URLs are left alone: linkify is another
    # dependency, and <https://nas.local:8080> already works.
    .enable(["table", "strikethrough"])
    # An image would be fetched from wherever it points, which on a LAN with no
    # internet is usually nowhere, and attaching images to cards is a feature of
    # its own. Without the rule, `![alt](url)` falls back to a plain link, so
    # the text still shows what was meant.
    .disable("image")
)


def _render_link_open(self, tokens, idx, options, env):
    """Open a link in a new tab, so following one doesn't leave the board.

    `rel` is explicit: browsers imply `noopener` for `target="_blank"`, but not
    every one we might be opened in does.
    """
    tokens[idx].attrSet("target", "_blank")
    tokens[idx].attrSet("rel", "noopener noreferrer")
    return self.renderToken(tokens, idx, options, env)


_MARKDOWN.add_render_rule("link_open", _render_link_open)


def render(text: str) -> Markup:
    """A card description as HTML, ready to drop into a template."""
    return Markup(_MARKDOWN.render(text or ""))
