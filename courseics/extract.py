"""HTML -> plain text lines.

The single most important rule in this module: when deleting a tag, insert a
newline for BLOCK-level tags and insert *nothing at all* for inline tags.

Google Sites (this project's only data source) splits a single run of text
across many <span> elements for styling. A real row on the lectures page is:

    <span>Oct</span><span>. 0</span><span>5</span><span>, 2026</span>

The obvious `re.sub('<[^>]+>', ' ', html)` turns that into "Oct . 0 5 , 2026",
which no sane date regex matches. That one whitespace decision was the
difference between finding 6 of 30 lecture dates and finding 30 of 30.
"""

import html as _html
import re
from typing import List

BLOCK_TAGS = (
    "p", "div", "li", "tr", "td", "th", "ul", "ol", "table", "tbody", "thead",
    "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header",
    "footer", "nav", "aside", "br", "hr", "blockquote", "pre", "dl", "dt", "dd",
)
_BLOCK = "(?:%s)" % "|".join(BLOCK_TAGS)

_DROP_ELEMENTS = re.compile(
    r"(?is)<(script|style|noscript|template|svg|head)\b[^>]*>.*?</\1\s*>")
_BLOCK_OPEN = re.compile(r"(?is)<" + _BLOCK + r"\b[^>]*>")
_BLOCK_CLOSE = re.compile(r"(?is)</" + _BLOCK + r"\s*>")
_ANY_TAG = re.compile(r"(?s)<[^>]*>")
_COMMENT = re.compile(r"(?s)<!--.*?-->")


def to_text(source_html: str) -> str:
    """Return the page's visible text, one logical block per line."""
    s = _COMMENT.sub(" ", source_html)
    s = _DROP_ELEMENTS.sub(" ", s)
    s = _BLOCK_OPEN.sub("\n", s)
    s = _BLOCK_CLOSE.sub("\n", s)
    # Inline tags (span, b, i, a, font, sup, ...) vanish with no separator.
    s = _ANY_TAG.sub("", s)
    s = _html.unescape(s)
    s = s.replace(" ", " ").replace("​", "")
    s = s.replace("\t", " ")
    s = re.sub(r"[ ]+", " ", s)
    lines = [ln.strip() for ln in s.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def to_lines(source_html: str) -> List[str]:
    text = to_text(source_html)
    return text.split("\n") if text else []
