"""Google OAuth: the parts that must hold without a network round trip."""

from base64 import urlsafe_b64encode
from hashlib import sha256

import pytest

from dashboard import auth_google as g


# ── open redirect ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("value", [
    "https://evil.example/steal",
    "//evil.example/steal",          # protocol-relative is still off-origin
    "http://localhost:8766/ok",      # absolute, even to ourselves
    "/path\\with\\backslash",
    "/path\nwith\nnewline",
    "", None, "relative/no/slash",
])
def test_return_to_rejects_anything_off_origin(value):
    # Without this the callback becomes an open redirect wearing our name.
    assert g.safe_return_to(value) == "/"


@pytest.mark.parametrize("value", ["/", "/budget", "/a/b?c=d"])
def test_return_to_allows_same_origin_paths(value):
    assert g.safe_return_to(value) == value


# ── configuration ─────────────────────────────────────────────────────────
def test_not_configured_without_a_client(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET_PATH", raising=False)
    assert not g.configured()


def test_secret_is_read_from_a_file(tmp_path, monkeypatch):
    """A secret in an env var is readable via docker inspect and /proc."""
    p = tmp_path / "secret"
    p.write_text("  s3cret\n")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET_PATH", str(p))
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    assert g.client_secret() == "s3cret"


def test_allowed_subs_is_closed_by_default(monkeypatch):
    # A public project's default deployment must not accept any Google
    # account on the internet.
    monkeypatch.delenv("ALLOWED_GOOGLE_SUBS", raising=False)
    assert g.allowed_subs() == set()


def test_allowed_subs_parses_a_list(monkeypatch):
    monkeypatch.setenv("ALLOWED_GOOGLE_SUBS", " 123 , 456 ,, ")
    assert g.allowed_subs() == {"123", "456"}


def test_begin_refuses_when_unconfigured(conn, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET_PATH", raising=False)
    with pytest.raises(g.OAuthError):
        g.begin(conn)


# ── the authorisation request ─────────────────────────────────────────────
@pytest.fixture
def configured(monkeypatch, tmp_path):
    p = tmp_path / "s"
    p.write_text("shhh")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET_PATH", str(p))
    monkeypatch.setenv("BASE_URL", "https://dash.example")
    return True


def _params(url):
    from urllib.parse import parse_qs, urlparse
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_begin_builds_a_pkce_request(conn, configured):
    url = g.begin(conn, purpose="signin")
    assert url.startswith(g.AUTH_URL)
    p = _params(url)
    assert p["code_challenge_method"] == "S256"
    assert p["response_type"] == "code"
    assert p["redirect_uri"] == "https://dash.example/api/auth/google/callback"
    assert p["access_type"] == "offline"


def test_the_challenge_is_the_hash_of_the_stored_verifier(conn, configured):
    url = g.begin(conn, purpose="signin")
    challenge = _params(url)["code_challenge"]
    row = conn.execute("SELECT code_verifier FROM oauth_pending").fetchone()
    expected = urlsafe_b64encode(
        sha256(row["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_signin_does_not_request_mail_or_calendar(conn, configured):
    """Nobody should hand over their inbox to look at a login page."""
    scopes = _params(g.begin(conn, purpose="signin"))["scope"].split()
    assert "openid" in scopes
    assert not any("gmail" in s or "calendar" in s for s in scopes)


def test_connect_asks_for_the_write_scopes(conn, configured):
    scopes = _params(g.begin(conn, purpose="connect"))["scope"].split()
    assert any("gmail.modify" in s for s in scopes)
    assert any("calendar.events" in s for s in scopes)


def test_unknown_purpose_is_refused(conn, configured):
    with pytest.raises(g.OAuthError):
        g.begin(conn, purpose="something-else")


def test_return_to_is_sanitised_at_the_point_it_is_stored(conn, configured):
    g.begin(conn, return_to="https://evil.example")
    assert conn.execute("SELECT return_to FROM oauth_pending"
                        ).fetchone()["return_to"] == "/"


# ── the pending record ────────────────────────────────────────────────────
def test_state_is_single_use(conn, configured):
    state = _params(g.begin(conn, purpose="signin"))["state"]
    assert g.take_pending(conn, state) is not None
    # A replayed callback must find nothing.
    assert g.take_pending(conn, state) is None


def test_unknown_state_is_rejected(conn, configured):
    assert g.take_pending(conn, "never-issued") is None


def test_expired_state_is_rejected_and_consumed(conn, configured):
    state = _params(g.begin(conn, purpose="signin"))["state"]
    conn.execute("UPDATE oauth_pending SET expires_at='2000-01-01T00:00:00+00:00'"
                 " WHERE state=?", (state,))
    conn.commit()
    assert g.take_pending(conn, state) is None
    assert conn.execute("SELECT COUNT(*) c FROM oauth_pending"
                        ).fetchone()["c"] == 0


def test_each_attempt_gets_fresh_secrets(conn, configured):
    a = _params(g.begin(conn, purpose="signin"))
    b = _params(g.begin(conn, purpose="signin"))
    assert a["state"] != b["state"]
    assert a["code_challenge"] != b["code_challenge"]


def test_purge_removes_only_expired(conn, configured):
    live = _params(g.begin(conn, purpose="signin"))["state"]
    dead = _params(g.begin(conn, purpose="signin"))["state"]
    conn.execute("UPDATE oauth_pending SET expires_at='2000-01-01T00:00:00+00:00'"
                 " WHERE state=?", (dead,))
    conn.commit()
    assert g.purge_expired(conn) == 1
    assert g.take_pending(conn, live) is not None


# ── the redirect URI Google must be told about ────────────────────────────
def test_redirect_uri_defaults_to_https(monkeypatch):
    """An http default guaranteed a redirect_uri_mismatch, because the
    installer always serves TLS and the browser requires it anyway."""
    monkeypatch.delenv("BASE_URL", raising=False)
    uri = g.redirect_uri()
    assert uri.startswith("https://"), uri
    assert uri.endswith("/api/auth/google/callback")


def test_a_saved_base_url_wins_over_the_environment(conn, monkeypatch, tmp_path):
    from dashboard import crypt, settings
    k = tmp_path / "k"
    k.write_text("a-long-random-secret-for-these-tests")
    monkeypatch.setenv("TOKEN_KEY_PATH", str(k))
    crypt.reset_for_tests()
    monkeypatch.setenv("BASE_URL", "https://from-the-environment:9999")
    settings.set_(conn, "base_url", "https://from-the-settings-page:8766")
    # Settings first, so a fix typed into the browser takes effect on the next
    # request rather than the next restart.
    assert g.redirect_uri(conn) == \
        "https://from-the-settings-page:8766/api/auth/google/callback"
    crypt.reset_for_tests()


def test_a_trailing_slash_does_not_double_up(conn, monkeypatch, tmp_path):
    """Google matches the redirect URI as an exact string, so a double slash
    is a mismatch and the message names neither the slash nor the cause."""
    from dashboard import crypt, settings
    k = tmp_path / "k"
    k.write_text("a-long-random-secret-for-these-tests")
    monkeypatch.setenv("TOKEN_KEY_PATH", str(k))
    crypt.reset_for_tests()
    settings.set_(conn, "base_url", "https://localhost:8766/")
    assert g.redirect_uri(conn) == \
        "https://localhost:8766/api/auth/google/callback"
    crypt.reset_for_tests()


def test_the_flow_sends_the_same_uri_the_page_displays(conn, monkeypatch,
                                                       tmp_path):
    """These drifting apart is the whole failure mode: you paste what the page
    shows into Google, and the flow sends something else."""
    import urllib.parse

    from dashboard import crypt, settings
    k = tmp_path / "k"
    k.write_text("a-long-random-secret-for-these-tests")
    monkeypatch.setenv("TOKEN_KEY_PATH", str(k))
    crypt.reset_for_tests()
    settings.set_(conn, "base_url", "https://localhost:8766")
    settings.set_(conn, "google_client_id", "cid.apps.googleusercontent.com")
    settings.set_(conn, "google_client_secret", "shh")

    url = g.begin(conn, purpose="signin")
    sent = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["redirect_uri"][0]
    assert sent == g.redirect_uri(conn)
    crypt.reset_for_tests()
