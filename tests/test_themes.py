"""Themes as data.

A theme arrives from outside and its values end up in CSS, so validation is
the whole security story. Most of these are about what is refused.
"""
import json

import pytest

from dashboard import builtin_themes, themes


def theme(**kw):
    base = {"name": "Test", "base": "dark", "tokens": {"--canvas": "#101010"}}
    base.update(kw)
    return json.dumps(base)


# ── what a value may be ───────────────────────────────────────────────────
@pytest.mark.parametrize("value", [
    "#fff", "#ffff", "#ea4b71", "#ea4b71cc",
    "rgb(1, 2, 3)", "rgba(234, 75, 113, 0.12)", "hsl(210 40% 20%)",
    "transparent", "currentColor",
])
def test_ordinary_colours_are_accepted(value):
    assert themes.validate_tokens({"--canvas": value}) == {"--canvas": value}


@pytest.mark.parametrize("value", [
    "url(https://evil/x.png)", "url('x')", "URL(x)",
    "red; background: url(x)", "#fff}", "#fff;", "expression(alert(1))",
    "javascript:x", "@import 'x'", "#fff/*c*/", "<script>", "#fff\\",
])
def test_anything_that_stops_being_a_value_is_refused(value):
    """CSP would already stop a remote url() loading. A value should never
    get that far."""
    with pytest.raises(themes.ThemeError):
        themes.validate_tokens({"--canvas": value})


@pytest.mark.parametrize("value", ["notacolour", "12px", "", "  ", "#12345"])
def test_a_colour_token_refuses_things_that_are_not_colours(value):
    with pytest.raises(themes.ThemeError):
        themes.validate_tokens({"--canvas": value})


def test_a_long_value_is_refused():
    with pytest.raises(themes.ThemeError, match="too long"):
        themes.validate_tokens({"--canvas": "#" + "a" * themes.MAX_VALUE})


# ── which tokens exist ────────────────────────────────────────────────────
def test_an_unknown_token_is_refused_not_ignored():
    """Silently dropping half a theme produces something that looks broken
    with no explanation, and the author cannot tell which half survived."""
    with pytest.raises(themes.ThemeError, match="not a token"):
        themes.validate_tokens({"--not-a-real-token": "#fff"})


def test_a_missing_double_dash_says_so():
    with pytest.raises(themes.ThemeError, match="leading --"):
        themes.validate_tokens({"canvas": "#fff"})


def test_layout_and_type_size_are_not_themeable():
    """A theme changes the look, not the layout. An imported theme that can
    set body text to eight pixels can make the application unusable."""
    for name in ("--s4", "--t-body", "--t-hero", "--gutter", "--fast"):
        assert name not in themes.SPEC, name
        with pytest.raises(themes.ThemeError):
            themes.validate_tokens({name: "8px"})


def test_fonts_shadows_and_radii_are_themeable():
    ok = themes.validate_tokens({
        "--font": 'Fraunces, "Iowan Old Style", Georgia, serif',
        "--mono": "ui-monospace, monospace",
        "--shadow": "0 2px 6px rgba(0,0,0,.3)",
        "--shadow-lg": "none",
        "--r": "10px", "--r-pill": "999px", "--track": "0.02em",
    })
    assert len(ok) == 7


def test_a_font_stack_cannot_smuggle_a_url():
    with pytest.raises(themes.ThemeError):
        themes.validate_tokens({"--font": "url(evil.woff2), serif"})


def test_a_vendor_prefixed_family_is_allowed():
    """-apple-system is a real family, and a pattern rejecting it rejects the
    stack this application ships."""
    assert themes.validate_tokens(
        {"--font": "ui-sans-serif, -apple-system, sans-serif"})


# ── the file ──────────────────────────────────────────────────────────────
def test_a_valid_theme_parses():
    t = themes.parse(theme(name="Painting", base="light"))
    assert t["name"] == "Painting" and t["base"] == "light"


@pytest.mark.parametrize("raw", [
    "not json", "[]", '"a string"', "null", "{}",
    '{"name": "x"}', '{"tokens": {"--canvas": "#fff"}}',
    '{"name": "x", "tokens": {}}',
    '{"name": "x", "base": "purple", "tokens": {"--canvas": "#fff"}}',
])
def test_malformed_theme_files_are_refused(raw):
    with pytest.raises(themes.ThemeError):
        themes.parse(raw)


def test_an_enormous_file_is_refused_before_parsing():
    with pytest.raises(themes.ThemeError, match="too large"):
        themes.parse("x" * (themes.MAX_JSON + 1))


# ── storage ───────────────────────────────────────────────────────────────
def test_the_builtin_is_seeded_and_valid(conn):
    got = themes.all_themes(conn)
    assert {t["name"] for t in got} == {"n8n Dark", "Painting"}
    assert all(t["builtin"] for t in got)
    # Each must survive its own validator, or it could not be re-imported.
    for t in got:
        themes.validate_tokens(t["tokens"])


def test_seeding_twice_does_not_duplicate(conn):
    from dashboard import schema
    schema.migrate(conn)
    schema.migrate(conn)
    assert len(themes.all_themes(conn)) == len(builtin_themes.ALL)


def test_a_builtin_cannot_be_deleted(conn):
    """Losing every theme would leave no way back to a known-good look."""
    builtin = themes.all_themes(conn)[0]
    with pytest.raises(themes.ThemeError, match="cannot be deleted"):
        themes.delete(conn, builtin["id"])


def test_an_imported_theme_can_be_deleted(conn):
    tid = themes.save(conn, themes.parse(theme(name="Mine")))
    themes.delete(conn, tid)
    assert {t["name"] for t in themes.all_themes(conn)} == {
        t["name"] for t in builtin_themes.ALL}


def test_deleting_something_that_is_not_there(conn):
    with pytest.raises(KeyError):
        themes.delete(conn, "0" * 32)


def test_a_theme_round_trips_through_export_and_import(conn):
    """Export, edit, import is how a new theme starts. If that loop is lossy
    the whole idea does not work."""
    original = themes.all_themes(conn)[0]
    exported = json.dumps(themes.export(original))
    reimported = themes.parse(exported)
    assert reimported["tokens"] == original["tokens"]
    assert reimported["base"] == original["base"]


def test_every_shipped_theme_validates():
    for t in builtin_themes.ALL:
        themes.parse(json.dumps(t))


def test_the_spec_matches_the_stylesheet():
    """A token in SPEC that the stylesheet does not use does nothing, and one
    the stylesheet uses that SPEC omits cannot be themed. Both are silent."""
    import pathlib
    import re
    css = pathlib.Path("app/src/dashboard/static/design-tokens.css").read_text()
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", css))
    unknown = set(themes.SPEC) - defined
    assert not unknown, "SPEC names tokens the stylesheet does not define: %s" % unknown


def test_the_preview_tokens_are_all_themeable():
    assert set(themes.PREVIEW) <= set(themes.SPEC)


# ── the Painting theme ────────────────────────────────────────────────────
def _rgb(h):
    h = h.lstrip("#")
    return [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]


def _lin(c):
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _lum(h):
    p = [_lin(c) for c in _rgb(h)]
    return 0.2126 * p[0] + 0.7152 * p[1] + 0.0722 * p[2]


def contrast(a, b):
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def painting():
    return next(t for t in builtin_themes.ALL if t["name"] == "Painting")


def test_painting_is_a_light_theme():
    assert painting()["base"] == "light"


def test_the_ink_is_black():
    """The brief, and the reason the rest of the palette is affordable."""
    assert painting()["tokens"]["--text"] == "#000000"


@pytest.mark.parametrize("token", [
    "--text", "--text-2", "--text-3", "--brand",
    "--ok", "--warn", "--danger", "--info",
])
def test_every_colour_that_carries_words_clears_aa(token):
    """Atmosphere is worth nothing if the inbox cannot be read at arm's
    length, which is where this is actually used."""
    t = painting()["tokens"]
    assert contrast(t[token], t["--surface"]) >= 4.5, (
        "%s is %.2f:1 on --surface" % (token, contrast(t[token], t["--surface"])))


def test_body_text_is_far_past_the_minimum():
    t = painting()["tokens"]
    assert contrast(t["--text"], t["--surface"]) >= 12


def test_the_dormant_ink_is_never_mistaken_for_readable():
    """--text-4 exists for marks nobody reads. If it happened to clear AA it
    would get used for something."""
    t = painting()["tokens"]
    assert contrast(t["--text-4"], t["--surface"]) < 4.5


def test_warn_and_danger_are_told_apart_by_lightness():
    """They are the two whose confusion actually costs something on a budget
    screen, and hue alone does not separate them for everyone."""
    t = painting()["tokens"]
    assert contrast(t["--warn"], t["--danger"]) >= 1.6


def test_the_canvas_texture_is_barely_there():
    """--canvas-dot is felt, not read. A visible weave is a distraction."""
    t = painting()["tokens"]
    assert 1.0 < contrast(t["--canvas"], t["--canvas-dot"]) < 1.3


def test_panels_sit_above_the_ground():
    t = painting()["tokens"]
    assert _lum(t["--surface"]) > _lum(t["--canvas"]), (
        "a painting has to be lighter than the wall to read as sitting on it")


def test_the_display_face_is_bundled_not_fetched():
    """font-src is 'self'. A theme naming a family that is not shipped and
    not on the device silently falls back."""
    import pathlib
    assert pathlib.Path(
        "app/src/dashboard/static/fonts/fraunces.woff2").exists()
    css = pathlib.Path("app/src/dashboard/static/styles.css").read_text()
    assert "@font-face" in css and "/fonts/fraunces.woff2" in css
    assert "Fraunces" in painting()["tokens"]["--font"]


def test_the_font_licence_ships_with_the_font():
    import pathlib
    lic = pathlib.Path(
        "app/src/dashboard/static/fonts/LICENSE-Fraunces.txt")
    assert lic.exists() and "Open Font License" in lic.read_text()


def test_the_widget_paint_follows_the_theme():
    """The blotches are masks, not pictures. A picture of one palette would
    stay that colour when the theme changed."""
    import pathlib
    css = pathlib.Path("app/src/dashboard/static/styles.css").read_text()
    i = css.index(".node.widget::after")
    block = css[i:i + 600]
    assert "mask:" in block
    assert "var(--surface)" in block, "the paint colour must come from a token"


def test_no_colour_escaped_the_token_system():
    """An element with a fixed colour ignores every theme -- which is exactly
    how the header stayed dark under a pale one."""
    import pathlib
    import re
    css = pathlib.Path("app/src/dashboard/static/styles.css").read_text()
    # Strip the data-URI masks: those are black-on-transparent shapes, and the
    # colour they end up is whatever token is masked through them.
    css = re.sub(r'url\("data:[^"]*"\)', "", css)
    hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", css)
    # What remains is deliberate: Google's own brand colours on the sign-in
    # button, and white on a saturated fill.
    allowed = {"#fff", "#ffffff", "#1f1f1f", "#dadce0", "#f7f8f8", "#000"}
    stray = sorted({h for h in hexes if h.lower() not in allowed})
    assert not stray, "colours outside the token system: %s" % stray
