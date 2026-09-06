"""Turning a Gmail message into readable text.

Email bodies are the most hostile input this application handles. They are
written by strangers, they are frequently machine-generated, and the HTML
variety carries tracking pixels, remote stylesheets and markup designed to
escape whatever renders it.

So none of it is ever rendered. This module reduces a message to plain text on
the server, and only that text reaches the browser -- which keeps the promise
made in static/dom.js, that nothing from outside is ever parsed as markup, and
keeps it without relying on the front end to be careful.

Preferring text/plain is not only about safety. Almost every real message
carries one, it is what the sender actually wrote, and it is shorter.
"""
import base64
import html
import re
from html.parser import HTMLParser

# Generous enough for any message worth reading, small enough that a runaway
# newsletter cannot fill the disk. Truncation is marked so a short body is
# never mistaken for the whole of one.
MAX_BODY = 256 * 1024
TRUNCATED = "\n\n[… truncated]"

# Tags whose *content* is not text at all. Dropping the tags alone would spill
# CSS and JavaScript source into the middle of the message.
SILENT = {"script", "style", "head", "title", "noscript", "template", "svg"}

# Without these an entire HTML mail collapses into one unreadable paragraph.
# Split in two because they are not the same thing: a paragraph deserves a
# blank line around it, a list item deserves a single break. Treating them
# alike double-spaces every list, which reads worse than no breaks at all.
PARA = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
        "section", "article", "header", "footer", "table", "ul", "ol",
        "pre", "hr"}
LINE = {"br", "tr", "li", "dt", "dd"}
# Cells need separating or a row reads as one run-on word: "Total$24.99".
# A gap rather than a break, because the row is one line.
CELL = {"td", "th"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._silent = 0

    def _break(self, tag):
        if tag in PARA:
            self.parts.append("\n\n")
        elif tag in LINE:
            self.parts.append("\n")
        elif tag in CELL:
            self.parts.append("  ")

    def handle_starttag(self, tag, attrs):
        if tag in SILENT:
            self._silent += 1
        else:
            self._break(tag)

    def handle_startendtag(self, tag, attrs):
        # <br/> and <hr/> never reach handle_starttag.
        self._break(tag)

    def handle_endtag(self, tag):
        if tag in SILENT:
            self._silent = max(0, self._silent - 1)
        elif tag in PARA:
            # Only paragraph-level tags break on both sides. Breaking on the
            # close of a line-level tag too would put a blank line between
            # every list item, which is the double-spacing this split exists
            # to avoid.
            self.parts.append("\n\n")

    def handle_data(self, data):
        if not self._silent:
            self.parts.append(data)

    def text(self):
        return "".join(self.parts)


def strip_html(markup):
    """HTML to text. Never returns markup, whatever it is given."""
    p = _Text()
    try:
        p.feed(str(markup or ""))
        p.close()
        out = p.text()
    except Exception:
        # A parser that gives up must not take the message with it: fall back
        # to removing anything angle-bracketed and unescaping the rest.
        out = html.unescape(re.sub(r"<[^>]*>", " ", str(markup or "")))
    return tidy(out)


def tidy(text):
    """Collapse the whitespace that survives any conversion."""
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    # \xa0 written as an escape rather than literally: &nbsp; becomes a real
    # non-breaking space after entity conversion, and it is invisible in a
    # source file, which is how it survives into output nobody meant to keep.
    text = re.sub("[ \t\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    # Three or more blank lines carry no more meaning than one.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def decode(data):
    """Gmail hands body data back base64url encoded, sometimes unpadded."""
    if not data:
        return ""
    raw = str(data).replace("-", "+").replace("_", "/")
    raw += "=" * (-len(raw) % 4)
    try:
        return base64.b64decode(raw).decode("utf-8", "replace")
    except Exception:
        return ""


def walk(payload):
    """Yield (mime_type, decoded_text) for every part, depth first."""
    if not isinstance(payload, dict):
        return
    mime = str(payload.get("mimeType") or "")
    body = payload.get("body") or {}
    if body.get("data"):
        yield mime, decode(body["data"])
    for part in payload.get("parts") or []:
        yield from walk(part)


def from_payload(payload):
    """The best readable text in a Gmail payload.

    text/plain wins when there is one: it is what the sender wrote, it is
    shorter, and it needs no conversion. HTML is the fallback, stripped.
    Attachments are ignored -- they have no body data in a metadata fetch and
    are not text in a full one.
    """
    plain, htmls = [], []
    for mime, text in walk(payload):
        if not text.strip():
            continue
        if mime.startswith("text/plain"):
            plain.append(text)
        elif mime.startswith("text/html"):
            htmls.append(text)

    if plain:
        out = tidy("\n\n".join(plain))
    elif htmls:
        out = strip_html("\n".join(htmls))
    else:
        return ""

    if len(out) > MAX_BODY:
        out = out[:MAX_BODY] + TRUNCATED
    return out
