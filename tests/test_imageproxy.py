"""Fetching remote images on the browser's behalf.

A server that fetches URLs on request is a liability unless it is fenced in.
Nearly every test here is about the fence.
"""
import pytest

from dashboard import imageproxy


# ── signing: not an open proxy ────────────────────────────────────────────
def test_a_signature_round_trips(conn):
    url = "https://cdn.example.com/a.jpg"
    assert imageproxy.verify(conn, url, imageproxy.sign(conn, url))


def test_a_signature_does_not_transfer_to_another_url(conn):
    """Otherwise one legitimate image is a key to fetching anything."""
    sig = imageproxy.sign(conn, "https://cdn.example.com/a.jpg")
    assert not imageproxy.verify(conn, "http://192.168.1.1/admin", sig)


@pytest.mark.parametrize("bad", ["", None, "x", "0" * 32])
def test_a_missing_or_wrong_signature_is_refused(conn, bad):
    assert not imageproxy.verify(conn, "https://cdn.example.com/a.jpg", bad)


def test_the_key_survives_a_reopen(conn):
    """A per-process key would break every image in every open message on
    each restart."""
    url = "https://cdn.example.com/a.jpg"
    sig = imageproxy.sign(conn, url)
    assert imageproxy.verify(conn, url, sig)
    # Same connection, fresh lookup -- the key comes from the database.
    assert imageproxy.sign(conn, url) == sig


def test_the_signed_path_is_self_hosted(conn):
    """It must satisfy img-src 'self'; a remote URL would simply not load."""
    path = imageproxy.signed_path(conn, "https://cdn.example.com/a.jpg")
    assert path.startswith("/api/image?")
    assert "cdn.example.com" in path      # url-encoded, but present


# ── where it refuses to go ────────────────────────────────────────────────
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x", "http://localhost/x", "https://127.0.0.1:8766/api",
    "http://10.0.0.1/x", "http://192.168.1.1/x", "http://172.16.0.1/x",
    "http://169.254.169.254/latest/meta-data/",     # cloud metadata
    "http://[::1]/x", "http://0.0.0.0/x",
])
def test_addresses_inside_the_network_are_refused(url):
    """This is the difference between a proxy and a way to read the router's
    admin page."""
    with pytest.raises(imageproxy.Refused):
        imageproxy.check(url)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://x/y", "gopher://x/y", "javascript:alert(1)",
    "data:text/html,x", "//example.com/x", "not a url",
])
def test_only_http_and_https_are_allowed(url):
    with pytest.raises(imageproxy.Refused):
        imageproxy.check(url)


def test_a_public_address_is_allowed():
    assert imageproxy.check("https://example.com/a.jpg") is True


def test_an_unresolvable_host_is_refused():
    with pytest.raises(imageproxy.Refused, match="resolve"):
        imageproxy.check("https://nx.invalid./a.jpg")


# ── what comes back ───────────────────────────────────────────────────────
class _Resp:
    def __init__(self, ctype, data, length=None):
        self.headers = {"Content-Type": ctype}
        if length is not None:
            self.headers["Content-Length"] = str(length)
        self._data = data

    def read(self, n=-1):
        return self._data[:n] if n and n > 0 else self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_a_non_image_is_refused(monkeypatch):
    """Otherwise this fetches HTML, JSON and anything else on request."""
    monkeypatch.setattr(imageproxy._opener, "open",
                        lambda *a, **k: _Resp("text/html", b"<html>"))
    with pytest.raises(imageproxy.Refused, match="not an image"):
        imageproxy.fetch("https://example.com/x")


def test_an_image_comes_back(monkeypatch):
    monkeypatch.setattr(imageproxy._opener, "open",
                        lambda *a, **k: _Resp("image/png", b"\x89PNG..."))
    ctype, data = imageproxy.fetch("https://example.com/a.png")
    assert ctype == "image/png" and data.startswith(b"\x89PNG")


def test_an_oversized_image_is_refused_by_declared_length(monkeypatch):
    monkeypatch.setattr(imageproxy._opener, "open", lambda *a, **k: _Resp(
        "image/png", b"x", length=imageproxy.MAX_BYTES + 1))
    with pytest.raises(imageproxy.Refused, match="too large"):
        imageproxy.fetch("https://example.com/a.png")


def test_a_lying_content_length_is_still_caught(monkeypatch):
    """Reading one byte past the cap is what makes the header non-binding."""
    monkeypatch.setattr(imageproxy._opener, "open", lambda *a, **k: _Resp(
        "image/png", b"x" * (imageproxy.MAX_BYTES + 10), length=10))
    with pytest.raises(imageproxy.Refused, match="too large"):
        imageproxy.fetch("https://example.com/a.png")


def test_the_content_type_parameters_are_ignored(monkeypatch):
    monkeypatch.setattr(imageproxy._opener, "open",
                        lambda *a, **k: _Resp("image/jpeg; charset=binary", b"j"))
    ctype, _ = imageproxy.fetch("https://example.com/a.jpg")
    assert ctype == "image/jpeg"


def test_a_redirect_into_the_network_is_refused():
    """A public URL that redirects to 127.0.0.1 walks past a check on the
    original, so the handler re-checks every hop."""
    handler = imageproxy._Guard()
    with pytest.raises(imageproxy.Refused):
        handler.redirect_request(None, None, 302, "Found", {},
                                 "http://127.0.0.1/admin")
