"""Claiming a fresh install.

This is the weakest moment in the system: the account created here can read
everything that follows. The tests are mostly about what must *not* work.
"""
import os

import pytest

from dashboard import auth, firstrun, storage


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "ROOT", str(tmp_path))
    yield tmp_path
    firstrun.clear_token()


def test_a_fresh_install_needs_setup(conn, data):
    assert firstrun.needs_setup(conn) is True
    assert firstrun.ensure_token(conn)


def test_the_token_file_is_not_world_readable(conn, data):
    firstrun.ensure_token(conn)
    mode = os.stat(firstrun.token_path()).st_mode & 0o777
    # Proving you can read this file stands in for owning the machine. That
    # only holds if other users on the box cannot read it.
    assert mode == 0o600, "setup token readable by other users: %o" % mode


def test_the_token_is_stable_until_used(conn, data):
    first = firstrun.ensure_token(conn)
    assert firstrun.ensure_token(conn) == first, \
        "token changed between reads -- the printed one would stop working"


def test_claiming_creates_the_user_and_a_session(conn, data):
    token = firstrun.ensure_token(conn)
    sid, csrf = firstrun.claim(conn, token, "king", "a-long-enough-password")
    assert sid and csrf
    assert auth.get_session(conn, sid) is not None
    assert firstrun.needs_setup(conn) is False


def test_the_token_is_destroyed_by_use(conn, data):
    token = firstrun.ensure_token(conn)
    firstrun.claim(conn, token, "king", "a-long-enough-password")
    assert not os.path.exists(firstrun.token_path()), \
        "setup token outlived the setup"
    assert firstrun.ensure_token(conn) is None


def test_a_second_claim_is_refused(conn, data):
    token = firstrun.ensure_token(conn)
    firstrun.claim(conn, token, "king", "a-long-enough-password")
    with pytest.raises(firstrun.SetupError, match="already been set up"):
        firstrun.claim(conn, token, "intruder", "another-long-password")


def test_a_wrong_token_is_refused(conn, data):
    firstrun.ensure_token(conn)
    with pytest.raises(firstrun.SetupError, match="not right"):
        firstrun.claim(conn, "not-the-token", "intruder", "a-long-password-x")
    assert firstrun.needs_setup(conn), "a bad code created a user anyway"


def test_an_empty_token_is_refused(conn, data):
    firstrun.ensure_token(conn)
    for bad in ("", None, " "):
        with pytest.raises(firstrun.SetupError):
            firstrun.claim(conn, bad, "intruder", "a-long-password-here")
    assert firstrun.needs_setup(conn)


def test_a_short_password_is_refused_before_the_token_is_spent(conn, data):
    token = firstrun.ensure_token(conn)
    with pytest.raises(firstrun.SetupError, match="at least"):
        firstrun.claim(conn, token, "king", "short")
    # Getting the password wrong must not burn the code -- otherwise a typo
    # means reinstalling.
    assert firstrun.ensure_token(conn) == token
    sid, _ = firstrun.claim(conn, token, "king", "a-long-enough-password")
    assert sid


def test_a_missing_username_is_refused(conn, data):
    token = firstrun.ensure_token(conn)
    with pytest.raises(firstrun.SetupError, match="username"):
        firstrun.claim(conn, token, "  ", "a-long-enough-password")
    assert firstrun.needs_setup(conn)


def test_only_one_of_two_simultaneous_claims_wins(conn, data):
    """Two people opening the page at once must not both get an account."""
    import sqlite3
    import threading

    from dashboard import schema
    path = str(data / "race.db")
    setup = sqlite3.connect(path)
    setup.row_factory = sqlite3.Row
    schema.migrate(setup)
    setup.close()

    token = firstrun.ensure_token(conn)
    # The lock is process-wide, so a shared connection is the honest test of
    # it; separate connections would be testing SQLite, not the guard.
    results = []

    def go(name):
        c = sqlite3.connect(path, timeout=5)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        try:
            firstrun.claim(c, token, name, "a-long-enough-password")
            results.append(("ok", name))
        except Exception as e:
            results.append(("no", str(e)))
        finally:
            c.close()

    threads = [threading.Thread(target=go, args=("a",)),
               threading.Thread(target=go, args=("b",))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wins = [r for r in results if r[0] == "ok"]
    assert len(wins) == 1, "both claims succeeded: %r" % (results,)
