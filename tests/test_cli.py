"""The command line, particularly the path that creates the first account."""

import io

import pytest

from dashboard import __main__ as cli
from dashboard import auth, storage


def test_create_user_reads_the_password_from_stdin(tmp_path, monkeypatch, capsys):
    """Not from argv.

    A password in a command line argument is visible in `ps` to every user on
    the machine, and lands in shell history.
    """
    monkeypatch.setattr("sys.stdin", io.StringIO("a decent passphrase\n"))
    rc = cli.main(["--data", str(tmp_path / "d"), "--create-user", "king"])
    assert rc == 0
    assert "created user king" in capsys.readouterr().out

    conn = storage.connect()
    assert auth.authenticate(conn, "king", "a decent passphrase") is not None
    assert auth.authenticate(conn, "king", "wrong") is None
    conn.close()


def test_create_user_refuses_an_empty_password(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    with pytest.raises(SystemExit):
        cli.main(["--data", str(tmp_path / "d"), "--create-user", "king"])


def test_data_root_is_created_and_private(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr("sys.stdin", io.StringIO("passphrase here\n"))
    root = tmp_path / "fresh"
    cli.main(["--data", str(root), "--create-user", "king"])
    assert (root / "dashboard.db").exists()
    assert os.stat(root).st_mode & 0o777 == 0o700


# ── --set-password ────────────────────────────────────────────────────────
def test_set_password_reads_from_stdin_and_changes_the_password(
        tmp_path, monkeypatch, capsys):
    d = str(tmp_path / "d")
    monkeypatch.setattr("sys.stdin", io.StringIO("the first passphrase\n"))
    cli.main(["--data", d, "--create-user", "king"])

    monkeypatch.setattr("sys.stdin", io.StringIO("the second passphrase\n"))
    rc = cli.main(["--data", d, "--set-password", "king"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "password changed for king" in out
    # The password itself must never be echoed.
    assert "the second passphrase" not in out

    conn = storage.connect()
    assert auth.authenticate(conn, "king", "the second passphrase") is not None
    assert auth.authenticate(conn, "king", "the first passphrase") is None
    conn.close()


def test_set_password_ends_that_users_sessions(tmp_path, monkeypatch):
    d = str(tmp_path / "d")
    monkeypatch.setattr("sys.stdin", io.StringIO("the first passphrase\n"))
    cli.main(["--data", d, "--create-user", "king"])

    conn = storage.connect()
    sid, _ = auth.login(conn, "king", "the first passphrase")
    conn.close()

    monkeypatch.setattr("sys.stdin", io.StringIO("the second passphrase\n"))
    cli.main(["--data", d, "--set-password", "king"])

    conn = storage.connect()
    assert auth.get_session(conn, sid) is None
    conn.close()


def test_set_password_refuses_an_empty_password(tmp_path, monkeypatch):
    d = str(tmp_path / "d")
    monkeypatch.setattr("sys.stdin", io.StringIO("a decent passphrase\n"))
    cli.main(["--data", d, "--create-user", "king"])
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    with pytest.raises(SystemExit):
        cli.main(["--data", d, "--set-password", "king"])


def test_set_password_refuses_one_that_is_too_short(tmp_path, monkeypatch):
    d = str(tmp_path / "d")
    monkeypatch.setattr("sys.stdin", io.StringIO("a decent passphrase\n"))
    cli.main(["--data", d, "--create-user", "king"])
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    with pytest.raises(SystemExit):
        cli.main(["--data", d, "--set-password", "king"])
    conn = storage.connect()
    assert auth.authenticate(conn, "king", "a decent passphrase") is not None
    conn.close()


def test_set_password_for_an_unknown_user_lists_the_real_ones(
        tmp_path, monkeypatch, capsys):
    """Naming them is fine: you already have shell access to the database."""
    d = str(tmp_path / "d")
    monkeypatch.setattr("sys.stdin", io.StringIO("a decent passphrase\n"))
    cli.main(["--data", d, "--create-user", "king"])
    monkeypatch.setattr("sys.stdin", io.StringIO("a decent passphrase\n"))
    with pytest.raises(SystemExit):
        cli.main(["--data", d, "--set-password", "nobody"])
    assert "king" in capsys.readouterr().err
