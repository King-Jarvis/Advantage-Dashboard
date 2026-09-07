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
    assert [t["name"] for t in got] == ["n8n Dark"]
    assert got[0]["builtin"] is True
    # It must survive its own validator, or it could not be re-imported.
    themes.validate_tokens(got[0]["tokens"])


def test_seeding_twice_does_not_duplicate(conn):
    from dashboard import schema
    schema.migrate(conn)
    schema.migrate(conn)
    assert len(themes.all_themes(conn)) == 1


def test_a_builtin_cannot_be_deleted(conn):
    """Losing every theme would leave no way back to a known-good look."""
    builtin = themes.all_themes(conn)[0]
    with pytest.raises(themes.ThemeError, match="cannot be deleted"):
        themes.delete(conn, builtin["id"])


def test_an_imported_theme_can_be_deleted(conn):
    tid = themes.save(conn, themes.parse(theme(name="Mine")))
    themes.delete(conn, tid)
    assert [t["name"] for t in themes.all_themes(conn)] == ["n8n Dark"]


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
