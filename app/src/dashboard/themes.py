"""Themes as data.

A theme is a validated map of design-token names to CSS values. Nothing about
it is code, so one can be written by hand, generated, or handed over by
somebody else and imported -- and applied at runtime by setting custom
properties on the root element, which the content security policy permits
where an injected stylesheet would not.

What a theme may change is the *look*: colour, type face, shadow, corner
radius. What it may not change is the spacing scale, the type sizes or the
motion timings. Those are layout and legibility, and a theme arriving from
outside that can set body text to eight pixels is a theme that can make the
application unusable. The line is drawn once, here, rather than argued about
per token.

Validation is an allowlist in both directions: an unknown token name is
refused, and a known one must match the pattern for its kind. The content
policy would already stop a remote url() from loading, but a value should
never get that far.
"""
import json
import re
import time
import uuid

MAX_JSON = 64 * 1024
MAX_VALUE = 400
MAX_NAME = 60

# Nothing legitimate in a token value contains any of these, and each of them
# is a way to stop being a value and start being something else.
FORBIDDEN = re.compile(r"url\s*\(|expression|javascript:|@import|[{};<>\\`]|/\*")

_HEX = r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})"
_NUM = r"-?(?:\d+\.?\d*|\.\d+)"
_FUNC = r"(?:rgb|rgba|hsl|hsla|color-mix|oklch|lab)\([^()]*\)"
_KEYWORD = r"transparent|currentColor|inherit|none"

COLOUR = re.compile(r"^\s*(?:%s|%s|%s)\s*$" % (_HEX, _FUNC, _KEYWORD))
LENGTH = re.compile(r"^\s*%s(?:px|rem|em|%%|ch)?\s*$" % _NUM)
# A shadow is a sequence of lengths and colours, possibly several, possibly
# inset. Written as a whitelist of what may appear rather than a grammar.
SHADOW = re.compile(
    r"^\s*(?:none|(?:inset\s+)?(?:%s(?:px|rem|em)?|\s|,|%s|%s)+)\s*$"
    % (_NUM, _HEX, _FUNC))
# Family names, quoted or bare, separated by commas. No url(), so no @font-face
# smuggled in -- a bundled face has to be declared in the stylesheet.
# The leading hyphen matters: -apple-system is a real family name, and a
# pattern that rejects it rejects the stack this application actually ships.
_FAMILY = r"(?:\"[^\"]{1,60}\"|'[^']{1,60}'|-?[A-Za-z][\w -]{0,60})"
FONT = re.compile(r"^\s*%s(?:\s*,\s*%s)*\s*$" % (_FAMILY, _FAMILY))

COLOURS = [
    "brand", "brand-hover", "brand-press", "brand-tint", "brand-edge",
    "canvas", "canvas-dot", "surface", "surface-2", "surface-3",
    "line", "line-lit", "text", "text-2", "text-3", "text-4",
    "ok", "ok-tint", "warn", "warn-tint", "danger", "danger-tint",
    "info", "info-tint",
    "node-agenda", "node-inbox", "node-budget", "node-system",
]

SPEC = {}
for name in COLOURS:
    SPEC["--" + name] = ("colour", COLOUR)
for name in ("r", "r-sm", "r-lg", "r-pill", "track"):
    SPEC["--" + name] = ("length", LENGTH)
for name in ("shadow", "shadow-lg"):
    SPEC["--" + name] = ("shadow", SHADOW)
for name in ("font", "mono"):
    SPEC["--" + name] = ("font", FONT)

# What a swatch shows, and the order it shows them in: enough to tell two
# themes apart at a glance without rendering the whole application.
PREVIEW = ["--canvas", "--surface", "--brand", "--text", "--ok", "--warn",
           "--danger", "--info"]


class ThemeError(Exception):
    pass


def validate_tokens(tokens):
    """Return a clean token map, or raise with the first thing wrong.

    Unknown names are refused rather than ignored: silently dropping half a
    theme produces something that looks broken with no explanation, and the
    author has no way to discover which half survived.
    """
    if not isinstance(tokens, dict):
        raise ThemeError("tokens must be an object")
    if not tokens:
        raise ThemeError("a theme with no tokens would change nothing")

    clean = {}
    for raw_name, raw_value in tokens.items():
        name = str(raw_name).strip()
        if name not in SPEC:
            known = "not a token this dashboard uses"
            if name.lstrip("-") in {k.lstrip("-") for k in SPEC}:
                known = "write it with the leading --"
            raise ThemeError("%s: %s" % (name[:MAX_NAME], known))
        value = str(raw_value).strip()
        if not value:
            raise ThemeError("%s has no value" % name)
        if len(value) > MAX_VALUE:
            raise ThemeError("%s is too long" % name)
        if FORBIDDEN.search(value):
            raise ThemeError("%s contains something that is not a value" % name)
        kind, pattern = SPEC[name]
        if not pattern.match(value):
            raise ThemeError("%s is not a valid %s: %s"
                             % (name, kind, value[:60]))
        clean[name] = value
    return clean


def parse(raw):
    """A theme from JSON text, fully validated."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if len(raw or "") > MAX_JSON:
        raise ThemeError("that file is too large to be a theme")
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise ThemeError("that is not valid JSON") from None
    if not isinstance(data, dict):
        raise ThemeError("a theme is an object with a name and tokens")

    name = str(data.get("name") or "").strip()[:MAX_NAME]
    if not name:
        raise ThemeError("a theme needs a name")
    base = str(data.get("base") or "dark").strip().lower()
    if base not in ("dark", "light"):
        raise ThemeError("base must be 'dark' or 'light'")
    return {
        "name": name,
        "author": str(data.get("author") or "").strip()[:MAX_NAME],
        "base": base,
        "tokens": validate_tokens(data.get("tokens")),
    }


# ── storage ───────────────────────────────────────────────────────────────
def _row(r):
    out = dict(r)
    out["tokens"] = json.loads(out.pop("tokens_json") or "{}")
    out["builtin"] = bool(out["builtin"])
    return out


def all_themes(conn):
    return [_row(r) for r in conn.execute(
        "SELECT * FROM themes ORDER BY builtin DESC, name")]


def get(conn, theme_id):
    r = conn.execute("SELECT * FROM themes WHERE id=?", (theme_id,)).fetchone()
    return _row(r) if r else None


def save(conn, theme, builtin=False):
    tid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO themes (id, name, author, base, tokens_json, builtin,"
        " created_at) VALUES (?,?,?,?,?,?,?)",
        (tid, theme["name"], theme.get("author", ""), theme["base"],
         json.dumps(theme["tokens"], separators=(",", ":")),
         1 if builtin else 0, time.strftime("%Y-%m-%dT%H:%M:%S")))
    conn.commit()
    return tid


def delete(conn, theme_id):
    row = conn.execute("SELECT builtin FROM themes WHERE id=?",
                       (theme_id,)).fetchone()
    if row is None:
        raise KeyError(theme_id)
    if row["builtin"]:
        # Otherwise it is possible to end up with no way back to a known-good
        # look, which for a theme system is the one unrecoverable state.
        raise ThemeError("the built-in themes cannot be deleted")
    conn.execute("DELETE FROM themes WHERE id=?", (theme_id,))
    conn.commit()


def export(theme):
    """The form that can be edited and imported again, or handed to someone."""
    return {"name": theme["name"], "author": theme.get("author", ""),
            "base": theme["base"], "tokens": theme["tokens"]}
