"""Email HTML as blocks.

The vocabulary is closed on purpose, so the tests are largely about what does
not get through it.
"""
import pytest

from dashboard import mailparts


def kinds(blocks):
    return [b["t"] for b in blocks]


def text_of(blocks):
    return " ".join("".join(r["s"] for r in b.get("runs", [])) for b in blocks)


# ── never markup ──────────────────────────────────────────────────────────
def test_script_and_style_never_become_blocks():
    blocks = mailparts.blocks_from_html(
        "<p>real</p><script>alert(1)</script><style>.a{}</style>")
    assert "alert" not in text_of(blocks) and ".a{" not in text_of(blocks)
    assert "real" in text_of(blocks)


def test_runs_carry_text_not_tags():
    blocks = mailparts.blocks_from_html("<p>hello <b>there</b></p>")
    joined = text_of(blocks)
    assert "hello" in joined and "there" in joined
    assert "<" not in joined and ">" not in joined


@pytest.mark.parametrize("href", [
    "javascript:alert(1)", "data:text/html,<script>x</script>",
    "vbscript:x", "file:///etc/passwd", "  javascript:x  ", "JaVaScRiPt:x",
])
def test_dangerous_hrefs_are_dropped(href):
    blocks = mailparts.blocks_from_html(f'<p><a href="{href}">click</a></p>')
    assert all("h" not in r for b in blocks for r in b.get("runs", [])), href


def test_ordinary_links_survive_with_their_label():
    blocks = mailparts.blocks_from_html(
        '<p>See <a href="https://example.com/x">the offer</a></p>')
    linked = [r for b in blocks for r in b.get("runs", []) if r.get("h")]
    assert linked and linked[0]["s"].strip() == "the offer"
    assert linked[0]["h"] == "https://example.com/x"


# ── images ────────────────────────────────────────────────────────────────
def test_a_real_image_becomes_an_image_block():
    blocks = mailparts.blocks_from_html(
        '<img src="https://cdn.example.com/a.jpg" alt="A hat">')
    img = [b for b in blocks if b["t"] == "img"]
    assert img and img[0]["src"].endswith("a.jpg") and img[0]["alt"] == "A hat"


@pytest.mark.parametrize("markup", [
    '<img src="https://t.example/p.gif" width="1" height="1">',
    '<img src="https://t.example/p.gif" width="1">',
    '<img src="https://t.example/p.gif" height="0">',
    '<img src="https://t.example/p.gif" width="1px" height="1px">',
])
def test_tracking_pixels_are_dropped(markup):
    assert [b for b in mailparts.blocks_from_html(markup) if b["t"] == "img"] == []


def test_a_data_uri_image_is_kept_as_is():
    """Already inline, allowed by the policy, and leaks nothing."""
    src = "data:image/png;base64,iVBORw0KGgo="
    blocks = mailparts.blocks_from_html(f'<img src="{src}">')
    assert next(b for b in blocks if b["t"] == "img")["src"] == src


@pytest.mark.parametrize("src", ["javascript:x", "cid:part1", "", "ftp://x/y"])
def test_images_we_cannot_fetch_are_dropped(src):
    blocks = mailparts.blocks_from_html(f'<img src="{src}">')
    assert [b for b in blocks if b["t"] == "img"] == []


# ── structure ─────────────────────────────────────────────────────────────
def test_headings_and_items_keep_their_kind():
    blocks = mailparts.blocks_from_html(
        "<h2>Deals</h2><ul><li>one</li><li>two</li></ul>")
    assert kinds(blocks) == ["h", "li", "li"]


def test_runs_sharing_a_link_are_folded():
    """A link split across three tags is one link, not three."""
    blocks = mailparts.blocks_from_html(
        '<p><a href="https://x.test/a"><b>Buy</b> <i>now</i></a></p>')
    linked = [r for b in blocks for r in b.get("runs", []) if r.get("h")]
    assert len(linked) == 1
    assert "Buy" in linked[0]["s"] and "now" in linked[0]["s"]


def test_empty_and_whitespace_blocks_are_dropped():
    blocks = mailparts.blocks_from_html("<p></p><div>   </div><p>real</p>")
    assert len(blocks) == 1 and text_of(blocks).strip() == "real"


def test_block_count_is_capped():
    blocks = mailparts.blocks_from_html("<p>x</p>" * (mailparts.MAX_BLOCKS + 200))
    assert len(blocks) <= mailparts.MAX_BLOCKS


def test_malformed_html_does_not_raise():
    for bad in ("<p>a<<<", "<img src=", "</p></div>", "<a href=>x</a>", ""):
        assert isinstance(mailparts.blocks_from_html(bad), list)


def test_a_payload_with_no_html_yields_no_blocks():
    import base64
    b64 = base64.urlsafe_b64encode(b"just text").decode().rstrip("=")
    payload = {"mimeType": "text/plain", "body": {"data": b64}}
    assert mailparts.blocks_from_payload(payload) == []
