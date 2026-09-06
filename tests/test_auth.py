"""Passwords, lockout and session lifetime."""

import time

import pytest

from dashboard import auth


def test_hash_is_salted_so_equal_passwords_differ(conn):
    h1, s1 = auth.hash_password("same password")
    h2, s2 = auth.hash_password("same password")
    assert s1 != s2 and h1 != h2, "unsalted hashes let one crack reveal many"


def test_verify_accepts_only_the_right_password(conn):
    h, s = auth.hash_password("correct horse")
    assert auth.verify_password("correct horse", h, s)
    assert not auth.verify_password("Correct horse", h, s)
    assert not auth.verify_password("", h, s)
    assert not auth.verify_password("correct horse", h, None)


def test_empty_passwords_are_refused(conn):
    with pytest.raises(ValueError):
        auth.hash_password("")


def test_a_user_needs_some_way_to_authenticate(conn):
    with pytest.raises(ValueError):
        auth.create_user(conn, "ghost")


def test_login_succeeds_and_issues_distinct_tokens(conn):
    auth.create_user(conn, "king", "hunter2hunter2")
    sid, csrf = auth.login(conn, "king", "hunter2hunter2")
    assert sid and csrf and sid != csrf
    assert len(sid) > 30 and len(csrf) > 30


def test_unknown_user_and_wrong_password_are_indistinguishable(conn):
    auth.create_user(conn, "king", "hunter2hunter2")
    assert auth.login(conn, "king", "wrong") is None
    assert auth.login(conn, "nobody", "wrong") is None


def test_unknown_user_still_costs_a_hash(conn):
    """Rejecting instantly would reveal that the username does not exist."""
    auth.create_user(conn, "king", "hunter2hunter2")
    t0 = time.perf_counter()
    auth.authenticate(conn, "definitely-not-a-user", "x")
    unknown = time.perf_counter() - t0
    t0 = time.perf_counter()
    auth.authenticate(conn, "king", "wrong")
    wrong = time.perf_counter() - t0
    # Within an order of magnitude is enough; the point is that the unknown
    # branch is not effectively free.
    assert unknown > wrong / 10


def test_lockout_after_repeated_failures(conn):
    auth.create_user(conn, "king", "hunter2hunter2")
    for _ in range(auth.MAX_FAILURES):
        assert auth.login(conn, "king", "wrong") is None
    # Correct password, but the account is now closed for a while.
    assert auth.login(conn, "king", "hunter2hunter2") is None
    row = auth.get_user(conn, "king")
    assert auth.is_locked(row)


def test_a_good_login_clears_the_failure_count(conn):
    auth.create_user(conn, "king", "hunter2hunter2")
    auth.login(conn, "king", "wrong")
    auth.login(conn, "king", "wrong")
    assert auth.get_user(conn, "king")["failed_count"] == 2
    auth.login(conn, "king", "hunter2hunter2")
    assert auth.get_user(conn, "king")["failed_count"] == 0


def test_session_round_trip(conn):
    uid = auth.create_user(conn, "king", "hunter2hunter2")
    sid, csrf = auth.create_session(conn, uid)
    row = auth.get_session(conn, sid)
    assert row["user_id"] == uid and row["csrf_token"] == csrf


def test_unknown_or_missing_session_is_none(conn):
    assert auth.get_session(conn, None) is None
    assert auth.get_session(conn, "") is None
    assert auth.get_session(conn, "not-a-session") is None


def test_absolute_expiry_is_not_extended_by_use(conn):
    """A stolen session must not be renewable indefinitely by using it."""
    uid = auth.create_user(conn, "king", "hunter2hunter2")
    sid, _ = auth.create_session(conn, uid)
    conn.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'"
                 " WHERE id=?", (sid,))
    conn.commit()
    assert auth.get_session(conn, sid) is None
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 0


def test_idle_sessions_close(conn):
    uid = auth.create_user(conn, "king", "hunter2hunter2")
    sid, _ = auth.create_session(conn, uid)
    conn.execute("UPDATE sessions SET last_seen_at='2000-01-01T00:00:00+00:00'"
                 " WHERE id=?", (sid,))
    conn.commit()
    assert auth.get_session(conn, sid) is None


def test_logging_in_again_does_not_reuse_the_session_id(conn):
    auth.create_user(conn, "king", "hunter2hunter2")
    a, _ = auth.login(conn, "king", "hunter2hunter2")
    b, _ = auth.login(conn, "king", "hunter2hunter2")
    assert a != b, "session ids must be rotated, never reused"


def test_destroying_user_sessions_logs_out_everywhere(conn):
    uid = auth.create_user(conn, "king", "hunter2hunter2")
    s1, _ = auth.create_session(conn, uid)
    s2, _ = auth.create_session(conn, uid)
    auth.destroy_user_sessions(conn, uid)
    assert auth.get_session(conn, s1) is None
    assert auth.get_session(conn, s2) is None


def test_purge_removes_only_expired(conn):
    uid = auth.create_user(conn, "king", "hunter2hunter2")
    live, _ = auth.create_session(conn, uid)
    dead, _ = auth.create_session(conn, uid)
    conn.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'"
                 " WHERE id=?", (dead,))
    conn.commit()
    assert auth.purge_expired(conn) == 1
    assert auth.get_session(conn, live) is not None


# ── resetting a password ──────────────────────────────────────────────────
def test_set_password_replaces_the_old_one(conn):
    auth.create_user(conn, "king", "hunter2hunter2")
    assert auth.set_password(conn, "king", "a-brand-new-password") is True
    assert auth.login(conn, "king", "hunter2hunter2") is None
    assert auth.login(conn, "king", "a-brand-new-password") is not None


def test_a_reset_signs_that_user_out_everywhere(conn):
    """A password is reset because it was forgotten or because it leaked. In
    the second case, leaving the old sessions alive changes nothing for
    whoever was already inside."""
    auth.create_user(conn, "king", "hunter2hunter2")
    sid, _ = auth.login(conn, "king", "hunter2hunter2")
    assert auth.get_session(conn, sid) is not None
    auth.set_password(conn, "king", "a-brand-new-password")
    assert auth.get_session(conn, sid) is None, "an old session outlived the reset"


def test_a_reset_does_not_touch_another_user(conn):
    auth.create_user(conn, "king", "hunter2hunter2")
    auth.create_user(conn, "other", "another-password")
    keep, _ = auth.login(conn, "other", "another-password")
    auth.set_password(conn, "king", "a-brand-new-password")
    assert auth.get_session(conn, keep) is not None
    assert auth.login(conn, "other", "another-password") is not None


def test_a_reset_clears_the_lockout(conn):
    """Locking someone out using a counter from before the reset would punish
    the wrong person."""
    auth.create_user(conn, "king", "hunter2hunter2")
    for _ in range(auth.MAX_FAILURES + 1):
        auth.login(conn, "king", "wrong")
    assert auth.is_locked(auth.get_user(conn, "king"))
    auth.set_password(conn, "king", "a-brand-new-password")
    row = auth.get_user(conn, "king")
    assert not auth.is_locked(row) and row["failed_count"] == 0
    assert auth.login(conn, "king", "a-brand-new-password") is not None


def test_a_short_password_is_refused(conn):
    auth.create_user(conn, "king", "hunter2hunter2")
    with pytest.raises(ValueError, match="at least"):
        auth.set_password(conn, "king", "short")
    # The old password must still work -- a rejected reset changes nothing.
    assert auth.login(conn, "king", "hunter2hunter2") is not None


def test_an_unknown_user_reports_rather_than_raising(conn):
    assert auth.set_password(conn, "nobody", "a-long-enough-password") is False


def test_the_cli_and_the_setup_form_agree_on_the_floor(conn):
    from dashboard import firstrun
    assert firstrun.MIN_PASSWORD == auth.MIN_PASSWORD
