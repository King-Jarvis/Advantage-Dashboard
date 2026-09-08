"""Connection and schema behaviour that is easy to assume and wrong to."""

import os
import sqlite3

import pytest

from dashboard import ledger, schema, storage


def test_migrate_is_idempotent(tmp_path):
    db = str(tmp_path / "d.db")
    c = storage.connect(db)
    assert schema.migrate(c) == schema.SCHEMA_VERSION
    assert schema.migrate(c) == schema.SCHEMA_VERSION
    n = c.execute("SELECT COUNT(*) c FROM meta WHERE key='schema_version'"
                  ).fetchone()["c"]
    assert n == 1, "re-running migrate must not duplicate the version row"


def test_foreign_keys_are_actually_enforced(tmp_path):
    """SQLite disables foreign keys by default, per connection.

    A schema full of REFERENCES clauses is decorative unless every connection
    turns them on -- which is why storage.connect() does, rather than relying
    on it having been set once somewhere.
    """
    c = storage.connect(str(tmp_path / "d.db"))
    schema.migrate(c)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO transactions (id, account_id, date, amount_cents,"
                  " created_at, updated_at) VALUES ('x','no-such-account',"
                  " '2026-01-01', -100, 'now', 'now')")


def test_wal_is_enabled(tmp_path):
    c = storage.connect(str(tmp_path / "d.db"))
    assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_data_root_is_private(tmp_path):
    root = str(tmp_path / "data")
    storage.configure(root)
    assert os.path.isdir(root)
    mode = os.stat(root).st_mode & 0o777
    # The database holds mail, calendar and finances.
    assert mode == 0o700, "data root should not be readable by other users"


def test_resolve_root_prefers_cli_then_env(monkeypatch):
    monkeypatch.setenv("DASHBOARD_DATA", "/from/env")
    assert storage.resolve_root("/from/cli") == "/from/cli"
    assert storage.resolve_root(None) == "/from/env"
    monkeypatch.delenv("DASHBOARD_DATA")
    assert storage.resolve_root(None) == storage.DEFAULT_ROOT


def test_a_real_file_survives_reopen(tmp_path):
    db = str(tmp_path / "d.db")
    c = storage.connect(db)
    schema.migrate(c)
    acct = ledger.create_account(c, "Checking")
    ledger.add_transaction(c, acct, "2026-01-05", -1234, "Shop")
    c.close()

    c2 = storage.connect(db)
    assert ledger.account_balance(c2, acct) == -1234
    assert ledger.check_invariants(c2) == []
    c2.close()


def test_log_writes_next_to_the_data(tmp_path):
    storage.configure(str(tmp_path / "d"))
    storage.log("hello")
    storage.log("world")
    body = open(storage.LOGFILE).read()
    assert "hello" in body and "world" in body
    assert body.count("\n") == 2


def test_log_survives_an_unwritable_path(tmp_path):
    # Logging must never be the thing that takes the process down.
    storage.LOGFILE = str(tmp_path / "no-such-dir" / "x.log")
    storage.log("should not raise")


def test_get_conn_is_one_connection_per_thread(tmp_path):
    import threading
    storage.configure(str(tmp_path / "d"))
    schema.migrate(storage.get_conn())
    a = storage.get_conn()
    assert storage.get_conn() is a, "same thread should reuse its connection"

    seen = []
    def worker():
        seen.append(storage.get_conn())
    t = threading.Thread(target=worker)
    t.start()
    t.join()
    # sqlite3 connections are not thread-safe; sharing one across threads is
    # how "recursive use of cursors" and silent corruption arrive.
    assert seen[0] is not a


def test_reconfiguring_drops_the_stale_connection(tmp_path):
    """A cached connection must not survive a change of data root.

    Otherwise every query after a reconfigure silently reads and writes the
    previous database while appearing to work.
    """
    storage.configure(str(tmp_path / "one"))
    schema.migrate(storage.get_conn())
    a = ledger.create_account(storage.get_conn(), "OnlyInFirst")

    storage.configure(str(tmp_path / "two"))
    schema.migrate(storage.get_conn())
    found = storage.get_conn().execute(
        "SELECT COUNT(*) c FROM accounts WHERE id=?", (a,)).fetchone()["c"]
    assert found == 0, "second database should not see the first one's rows"


def test_database_and_log_are_owner_only(tmp_path):
    """A file holding mail and bank records should not be world-readable.

    The directory is 0700, which is the real containment. These modes are
    what travels with the file when a backup or restore copies it somewhere
    less careful.
    """
    import os
    import stat

    from dashboard import storage

    root = str(tmp_path / "data")
    storage.configure(root)
    conn = storage.connect()
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    storage.log("hello")

    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    for name in ("dashboard.db", "dashboard.log"):
        mode = stat.S_IMODE(os.stat(os.path.join(root, name)).st_mode)
        assert mode == 0o600, "%s is %o" % (name, mode)
    conn.close()
