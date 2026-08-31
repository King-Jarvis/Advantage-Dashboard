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
