"""Settings storage, and that secrets stay secret."""


import pytest

from dashboard import crypt, settings


@pytest.fixture(autouse=True)
def key(tmp_path, monkeypatch):
    p = tmp_path / "token_key"
    p.write_text("a-long-random-secret-standing-in-for-bootstrap")
    monkeypatch.setenv("TOKEN_KEY_PATH", str(p))
    crypt.reset_for_tests()
    yield
    crypt.reset_for_tests()


# ── ordinary settings ─────────────────────────────────────────────────────
def test_unset_settings_return_their_default(conn):
    assert settings.get(conn, "baseline_window_months") == 12
    assert settings.get(conn, "enable_llm_categories") is False


def test_round_trip_by_type(conn):
    settings.set_(conn, "baseline_window_months", "18")
    assert settings.get(conn, "baseline_window_months") == 18
    settings.set_(conn, "enable_llm_categories", "true")
    assert settings.get(conn, "enable_llm_categories") is True
    settings.set_(conn, "enable_llm_categories", False)
    assert settings.get(conn, "enable_llm_categories") is False
    settings.set_(conn, "currency_symbol", "£")
    assert settings.get(conn, "currency_symbol") == "£"


def test_a_non_numeric_int_is_refused(conn):
    with pytest.raises(ValueError):
        settings.set_(conn, "baseline_window_months", "many")


def test_unknown_keys_are_refused(conn):
    # A settings table that accepts anything becomes a place bugs hide.
    with pytest.raises(KeyError):
        settings.set_(conn, "not_a_setting", "x")
    with pytest.raises(KeyError):
        settings.get(conn, "not_a_setting")


def test_clearing_returns_to_the_default(conn):
    settings.set_(conn, "timezone", "Europe/London")
    settings.clear(conn, "timezone")
    assert settings.get(conn, "timezone") == "UTC"


# ── secrets ───────────────────────────────────────────────────────────────
def test_a_secret_is_not_stored_in_plaintext(conn):
    settings.set_(conn, "anthropic_api_key", "FAKE-ANTHROPIC-KEY")
    raw = conn.execute("SELECT value FROM settings WHERE key='anthropic_api_key'"
                       ).fetchone()["value"]
    assert "sk-ant" not in raw
    assert settings.get(conn, "anthropic_api_key") == "FAKE-ANTHROPIC-KEY"


def test_a_secret_is_never_returned_for_display(conn):
    settings.set_(conn, "anthropic_api_key", "FAKE-ANTHROPIC-KEY")
    shown = {s["key"]: s for s in settings.all_for_display(conn)}
    item = shown["anthropic_api_key"]
    # Enough to render a field, and nothing a stolen session could harvest.
    assert item["value"] is None
    assert item["is_set"] is True


def test_an_unset_secret_reports_as_unset(conn):
    shown = {s["key"]: s for s in settings.all_for_display(conn)}
    assert shown["anthropic_api_key"]["is_set"] is False


def test_storing_a_secret_without_a_key_refuses_rather_than_downgrading(
        conn, monkeypatch):
    """A silent fallback to plaintext would be the worst possible outcome."""
    monkeypatch.delenv("TOKEN_KEY_PATH", raising=False)
    monkeypatch.delenv("TOKEN_KEY", raising=False)
    crypt.reset_for_tests()
    assert crypt.available() is False
    with pytest.raises(RuntimeError):
        settings.set_(conn, "anthropic_api_key", "FAKE-KEY")
    assert conn.execute("SELECT COUNT(*) c FROM settings").fetchone()["c"] == 0


def test_a_rotated_key_makes_old_secrets_unreadable_not_wrong(
        conn, tmp_path, monkeypatch):
    settings.set_(conn, "anthropic_api_key", "FAKE-KEY-ORIGINAL")
    other = tmp_path / "other_key"
    other.write_text("a-completely-different-secret-value-here")
    monkeypatch.setenv("TOKEN_KEY_PATH", str(other))
    crypt.reset_for_tests()
    # Better to prompt again than to authenticate with nonsense.
    assert settings.get(conn, "anthropic_api_key") == ""


# ── environment export ────────────────────────────────────────────────────
def test_settings_reach_the_code_that_reads_environment_variables(conn):
    settings.set_(conn, "enable_llm_categories", True)
    settings.set_(conn, "anthropic_api_key", "FAKE-KEY-2")
    settings.set_(conn, "classify_model", "claude-haiku-4-5")
    env = settings.export_env(conn)
    assert env["ENABLE_LLM_CATEGORIES"] == "true"
    assert env["ANTHROPIC_API_KEY"] == "FAKE-KEY-2"
    assert env["CLASSIFY_MODEL"] == "claude-haiku-4-5"


# ── connected Google accounts ─────────────────────────────────────────────
def test_connecting_an_account_encrypts_its_refresh_token(conn):
    aid = settings.save_google_account(
        conn, sub="1234", email="a@example.com",
        refresh_token="FAKE-REFRESH-TOKEN", access_token="FAKE-ACCESS-TOKEN",
        expires_at="2026-01-01T00:00:00", scopes="gmail.modify")
    raw = conn.execute("SELECT refresh_token FROM google_accounts WHERE id=?",
                       (aid,)).fetchone()["refresh_token"]
    assert "1//" not in raw
    assert settings.google_refresh_token(conn, aid) == "FAKE-REFRESH-TOKEN"


def test_listing_accounts_never_includes_tokens(conn):
    settings.save_google_account(conn, "1234", "a@example.com", "FAKE-REFRESH",
                                 "FAKE-ACCESS", None, "gmail.modify")
    listed = settings.list_google_accounts(conn)
    blob = str(listed)
    assert "FAKE-REFRESH" not in blob and "FAKE-ACCESS" not in blob
    assert listed[0]["email"] == "a@example.com"


def test_reconnecting_without_a_refresh_token_keeps_the_stored_one(conn):
    """Google sends a refresh token only on first consent."""
    aid = settings.save_google_account(
        conn, "1234", "a@example.com", "FAKE-REFRESH-ORIGINAL",
        "FAKE-ACCESS", None, "s")
    settings.save_google_account(conn, "1234", "a@example.com", "",
                                 "FAKE-ACCESS-2", None, "s")
    assert settings.google_refresh_token(conn, aid) == "FAKE-REFRESH-ORIGINAL"


def test_disconnecting_forgets_the_account_entirely(conn):
    aid = settings.save_google_account(conn, "1234", "a@example.com",
                                       "FAKE-REFRESH", "FAKE-ACCESS", None, "s")
    assert settings.disconnect_google(conn, aid) == 1
    assert settings.list_google_accounts(conn) == []
    assert settings.google_refresh_token(conn, aid) is None


def test_sync_errors_are_recorded_against_the_account(conn):
    aid = settings.save_google_account(conn, "1234", "a@example.com",
                                       "FAKE-REFRESH", "", None, "s")
    settings.note_sync(conn, aid, error="invalid_grant")
    assert settings.list_google_accounts(conn)[0]["last_error"] == "invalid_grant"
