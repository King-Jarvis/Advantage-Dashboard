"""Schema changes have to reach databases that already hold data.

CREATE TABLE IF NOT EXISTS does nothing to an existing table, so every one of
these tests is really asking the same question: would this change actually
appear on the installation that has been running for a month?
"""
import sqlite3

import pytest

from dashboard import schema


def test_every_added_column_lands(conn):
    for table, name, _ in schema.ADDED_COLUMNS:
        assert schema.has_column(conn, table, name), f"{table}.{name} missing"


def test_migrate_is_idempotent(conn):
    before = schema.migrate(conn)
    after = schema.migrate(conn)
    assert before == after == schema.SCHEMA_VERSION


def test_adding_a_column_twice_is_harmless(conn):
    assert schema.add_column(conn, "messages", "trashed",
                             "INTEGER NOT NULL DEFAULT 0") is False


def test_a_genuinely_new_column_is_added(conn):
    assert schema.has_column(conn, "messages", "zzz_probe") is False
    assert schema.add_column(conn, "messages", "zzz_probe", "TEXT") is True
    assert schema.has_column(conn, "messages", "zzz_probe") is True


def test_existing_rows_survive_with_sane_defaults(tmp_path):
    """The point of the exercise: real rows must still be there afterwards,
    and the new columns must not read as NULL where code expects a number."""
    path = tmp_path / "live.db"
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    schema.migrate(c)
    c.execute("INSERT INTO google_accounts (id, sub, email, connected_at)"
              " VALUES ('a','s','e@example.com','2026-01-01T00:00:00')")
    c.execute("INSERT INTO messages (id, account_id, source_uid, subject,"
              " received_at) VALUES ('m','a','u','Hello','2026-01-01T00:00:00')")
    c.commit()
    c.close()

    # Reopen and migrate again, the way a restart after an upgrade would.
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    schema.migrate(c)
    row = c.execute("SELECT * FROM messages WHERE id='m'").fetchone()
    assert row["subject"] == "Hello"
    assert row["trashed"] == 0 and row["is_spam"] == 0
    assert row["push_error"] == ""
    assert row["body_text"] is None
    c.close()


def test_the_version_was_bumped():
    """A migration that does not bump the version is invisible to anything
    that reasons about upgrades."""
    assert schema.SCHEMA_VERSION >= 2


@pytest.mark.parametrize("table,name,decl", schema.ADDED_COLUMNS)
def test_each_declaration_is_valid_sql(tmp_path, table, name, decl):
    c = sqlite3.connect(":memory:")
    schema.migrate(c)
    c.execute("ALTER TABLE %s ADD COLUMN probe_%s %s" % (table, name, decl))
    c.close()
