"""Email HTML as a list of blocks, never as markup.

The plain-text alternative is safe and readable, but it is also where a
newsletter turns into bare URLs and orphaned price fragments -- the sender put
the meaning in pictures and link text, and the text part throws both away.

So this reads the HTML part instead and reduces it to a small, closed
vocabulary of blocks: headings, paragraphs, list items, images. Text and links
travel as data, and the front end builds real DOM nodes from them. At no point
does markup from a stranger reach a browser, which is the same guarantee
mailtext gives, arrived at from the other direction.

Two things are deliberately dropped. Anything not in the vocabulary is
flattened to its text, because a block type nobody renders is a block type
that silently disappears. And declared 1x1 images go, because they are
tracking pixels and nothing else.
"""
import re
from html.parser import HTMLParser

from .mailtext import SILENT, tidy, walk

MAX_BLOCKS = 400
MAX_TEXT = 4000          # per run
MAX_TOTAL = 256 * 1024

HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
# Block-level only. b, i, span, font and friends are inline: ending a block
# on them splits "<b>Buy</b> <i>now</i>" into two paragraphs and, worse, into
# two separate links.
PARAGRAPHS = {"p", "div", "blockquote", "section", "article", "td", "tr",
              "table", "header", "footer", "body", "html", "center"}
ITEMS = {"li", "dd", "dt"}


def _clean_href(value):
    """Only ordinary web links survive.

    javascript:, data: and vbscript: hrefs are the reason link handling is a
    security question at all. They cannot do anything here -- the front end
    builds an anchor and sets href through the DOM, and CSP forbids inline
    script -- but a link that cannot work should not be offered.
    """
    href = str(value or "").strip()
    if not re.match(r"^https?://", href, re.I):
        return ""
    return href[:2000]


def _clean_src(value):
    src = str(value or "").strip()
    # data: images are already inline and allowed by the policy, so they need
    # no proxy and leak nothing.
    if src.lower().startswith("data:image/"):
        return src[:MAX_TOTAL]
    if not re.match(r"^https?://", src, re.I):
        return ""
    return src[:2000]


def _is_pixel(attrs):
    """A declared 1x1 is a tracker, not a picture."""
    for key in ("width", "height"):
        raw = attrs.get(key, "")
        m = re.match(r"^\s*(\d+)", str(raw))
        if m and int(m.group(1)) <= 2:
            return True
    return False


class _Blocks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self._runs = []
        self._silent = 0
        self._href = ""
        self._kind = "p"

    # ── assembling ────────────────────────────────────────────────────────
    def _flush(self, kind=None):
        runs = [r for r in self._runs if r["s"].strip()]
        self._runs = []
        if runs and len(self.blocks) < MAX_BLOCKS:
            self.blocks.append({"t": kind or self._kind, "runs": runs})
        self._kind = "p"

    def _emit(self, block):
        if len(self.blocks) < MAX_BLOCKS:
            self.blocks.append(block)

    # ── parsing ───────────────────────────────────────────────────────────
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in SILENT:
            self._silent += 1
            return
        if self._silent:
            return

        if tag == "img":
            src = _clean_src(a.get("src"))
            if src and not _is_pixel(a):
                self._flush()
                self._emit({"t": "img", "src": src,
                            "alt": str(a.get("alt") or "")[:300]})
            return
        if tag == "a":
            self._href = _clean_href(a.get("href"))
            return
        if tag == "br":
            self._runs.append({"s": "\n"})
            return
        if tag == "hr":
            self._flush()
            self._emit({"t": "hr"})
            return
        if tag in HEADINGS:
            self._flush()
            self._kind = "h"
            return
        if tag in ITEMS:
            self._flush()
            self._kind = "li"
            return
        if tag in PARAGRAPHS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in SILENT:
            self._silent = max(0, self._silent - 1)
            return
        if self._silent:
            return
        if tag == "a":
            self._href = ""
            return
        if tag in HEADINGS:
            self._flush("h")
        elif tag in ITEMS:
            self._flush("li")
        elif tag in PARAGRAPHS:
            self._flush()

    def handle_data(self, data):
        if self._silent:
            return
        text = str(data or "")
        if not text.strip():
            # Keep a single separating space; drop the rest of the whitespace
            # that HTML source is full of.
            if self._runs and not self._runs[-1]["s"].endswith((" ", "\n")):
                self._runs.append({"s": " "})
            return
        run = {"s": text[:MAX_TEXT]}
        if self._href:
            run["h"] = self._href
        self._runs.append(run)

    def finish(self):
        self._flush()
        return self.blocks


def _tidy_blocks(blocks):
    """Merge each block's runs and drop what carries nothing."""
    out = []
    for b in blocks:
        if b["t"] in ("img", "hr"):
            out.append(b)
            continue
        merged = []
        for run in b["runs"]:
            text = tidy(run["s"]) if "\n" in run["s"] else run["s"]
            text = re.sub(r"[ \t\xa0]+", " ", text)
            if not text.strip():
                # Whitespace between two runs is a word gap, not nothing.
                # Dropping it outright turns "Buy now" into "Buynow".
                if merged and not merged[-1]["s"].endswith((" ", "\n")):
                    merged[-1]["s"] += " "
                continue
            # Fold consecutive runs that share a destination, so a link split
            # across three tags is one link rather than three.
            if merged and merged[-1].get("h") == run.get("h"):
                merged[-1]["s"] += text
            else:
                merged.append({**run, "s": text})
        for run in merged:
            run["s"] = run["s"].strip() if len(merged) == 1 else run["s"]
        if merged:
            out.append({**b, "runs": merged})
    return out


def blocks_from_html(markup):
    p = _Blocks()
    try:
        p.feed(str(markup or ""))
        p.close()
        raw = p.finish()
    except Exception:
        return []
    return _tidy_blocks(raw)[:MAX_BLOCKS]


def blocks_from_payload(payload):
    """The richest readable form of a message.

    HTML is preferred here, which is the opposite of mailtext's choice and for
    the opposite reason: this function exists to keep the pictures and the
    link text that the plain part discards.
    """
    htmls = [t for m, t in walk(payload) if m.startswith("text/html") and t.strip()]
    if not htmls:
        return []
    return blocks_from_html("\n".join(htmls)[:MAX_TOTAL])
