"""Reducing email to text.

Email bodies are the most hostile input the application takes. These tests are
mostly about what must never come out the other end.
"""
import base64

import pytest

from dashboard import mailtext


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def part(mime, text):
    return {"mimeType": mime, "body": {"data": b64(text)}}


# ── never markup ──────────────────────────────────────────────────────────
def test_script_content_never_survives():
    """Dropping the tag but keeping its content would spill JavaScript into
    the middle of the message."""
    out = mailtext.strip_html(
        "<p>Hello</p><script>alert('x'); var a = 1;</script><p>Bye</p>")
    assert "alert" not in out and "var a" not in out
    assert "Hello" in out and "Bye" in out


def test_style_content_never_survives():
    out = mailtext.strip_html("<style>.a{color:red}</style><p>Text</p>")
    assert "color:red" not in out and ".a{" not in out
    assert "Text" in out


@pytest.mark.parametrize("markup", [
    "<p>a</p>", "<img src=x onerror=alert(1)>", "<a href='javascript:x'>l</a>",
    "<div><span>nested</span></div>", "<!-- comment -->text",
    "<iframe src='http://evil'></iframe>hi", "<svg><script>x</script></svg>",
])
def test_no_angle_brackets_ever_come_back(markup):
    out = mailtext.strip_html(markup)
    assert "<" not in out and ">" not in out, out


def test_malformed_markup_still_yields_text():
    """A parser that gives up must not take the message with it.

    A stray angle bracket in the *text* is fine and is what the sender typed;
    what must not survive is a tag. Since everything reaches the browser
    through textContent, a literal '<' is a character, not markup.
    """
    out = mailtext.strip_html("<p>start <<<>>> <b>bold</p></unclosed")
    assert "start" in out and "bold" in out
    assert "<b>" not in out and "<p>" not in out


def test_entities_are_decoded():
    out = mailtext.strip_html("<p>Tom &amp; Jerry &lt;3 &nbsp; caf&eacute;</p>")
    assert "Tom & Jerry" in out and "<3" in out and "café" in out
    assert "&amp;" not in out and "\xa0" not in out


# ── readability ───────────────────────────────────────────────────────────
def test_paragraphs_get_a_blank_line():
    """Without this an entire HTML mail is one unreadable paragraph."""
    assert mailtext.strip_html("<p>One</p><p>Two</p>") == "One\n\nTwo"


def test_list_items_get_a_single_break_not_a_blank_line():
    """Double-spacing every list reads worse than not breaking at all."""
    out = mailtext.strip_html("<ul><li>one</li><li>two</li><li>three</li></ul>")
    assert out == "one\ntwo\nthree"


def test_br_breaks_the_line():
    assert mailtext.strip_html("a<br>b<br/>c") == "a\nb\nc"


def test_runs_of_blank_lines_collapse():
    assert "\n\n\n" not in mailtext.tidy("a\n\n\n\n\n\nb")


def test_leading_and_trailing_space_goes():
    assert mailtext.tidy("   \n\n hello \n\n  ") == "hello"


# ── choosing a part ───────────────────────────────────────────────────────
def test_plain_text_is_preferred_over_html():
    payload = {"mimeType": "multipart/alternative", "parts": [
        part("text/plain", "the plain one"),
        part("text/html", "<p>the html one</p>")]}
    assert mailtext.from_payload(payload) == "the plain one"


def test_html_is_used_when_there_is_no_plain_part():
    payload = {"mimeType": "text/html",
               "body": {"data": b64("<p>only html</p>")}}
    assert mailtext.from_payload(payload) == "only html"


def test_nested_multipart_is_walked():
    payload = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "multipart/alternative", "parts": [
            part("text/plain", "buried deep")]},
        {"mimeType": "application/pdf", "body": {"attachmentId": "x"}}]}
    assert mailtext.from_payload(payload) == "buried deep"


def test_an_attachment_only_message_yields_nothing():
    payload = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "image/png", "body": {"attachmentId": "x"}}]}
    assert mailtext.from_payload(payload) == ""


def test_empty_and_missing_payloads_are_safe():
    for bad in (None, {}, {"parts": []}, {"body": {}}, "not a dict", []):
        assert mailtext.from_payload(bad) == ""


def test_whitespace_only_parts_are_ignored():
    payload = {"parts": [part("text/plain", "   \n  "),
                         part("text/html", "<p>real content</p>")]}
    assert mailtext.from_payload(payload) == "real content"


# ── decoding ──────────────────────────────────────────────────────────────
def test_base64url_without_padding_decodes():
    assert mailtext.decode(b64("hello there")) == "hello there"


def test_base64_with_url_characters_decodes():
    raw = base64.urlsafe_b64encode(b"a?b>c~d").decode()
    assert mailtext.decode(raw.rstrip("=")) == "a?b>c~d"


def test_undecodable_data_returns_empty_rather_than_raising():
    assert mailtext.decode("!!!not base64!!!") == ""
    assert mailtext.decode(None) == ""


def test_invalid_utf8_is_replaced_not_fatal():
    raw = base64.urlsafe_b64encode(b"good \xff\xfe bad").decode().rstrip("=")
    assert "good" in mailtext.decode(raw)


# ── size ──────────────────────────────────────────────────────────────────
def test_a_huge_body_is_truncated_and_says_so():
    """A short body must never be mistaken for the whole of one."""
    payload = {"mimeType": "text/plain",
               "body": {"data": b64("x" * (mailtext.MAX_BODY + 5000))}}
    out = mailtext.from_payload(payload)
    assert len(out) <= mailtext.MAX_BODY + len(mailtext.TRUNCATED)
    assert out.endswith(mailtext.TRUNCATED)


def test_a_normal_body_is_not_marked_truncated():
    payload = {"mimeType": "text/plain", "body": {"data": b64("short")}}
    assert mailtext.from_payload(payload) == "short"


def test_table_cells_do_not_run_together():
    """A row of cells must not read as one word: 'Total$24.99'."""
    out = mailtext.strip_html(
        "<table><tr><td>Total</td><td>$24.99</td></tr></table>")
    assert "Total" in out and "$24.99" in out
    assert "Total$24.99" not in out


def test_rows_are_separate_lines():
    out = mailtext.strip_html(
        "<table><tr><td>a</td></tr><tr><td>b</td></tr></table>")
    assert out.splitlines() == ["a", "b"]
