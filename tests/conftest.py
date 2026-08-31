import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app", "src"))

from dashboard import ledger, schema


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    schema.migrate(c)
    yield c
    # Every test ends with the ledger internally consistent. A test that
    # leaves it broken has found a bug even if its own assertions passed.
    violations = ledger.check_invariants(c)
    assert violations == [], "invariants violated at teardown: %s" % violations
    c.close()


@pytest.fixture
def book(conn):
    """A minimal chart of accounts and categories."""
    g_exp = ledger.create_category_group(conn, "Everyday")
    g_inc = ledger.create_category_group(conn, "Income", is_income=True)
    return {
        "checking": ledger.create_account(conn, "Checking"),
        "savings": ledger.create_account(conn, "Savings"),
        "groceries": ledger.create_category(conn, g_exp, "Groceries"),
        "fuel": ledger.create_category(conn, g_exp, "Fuel"),
        "rent": ledger.create_category(conn, g_exp, "Rent",
                                       carryover_negative=True),
        "salary": ledger.create_category(conn, g_inc, "Salary", is_income=True),
    }


@pytest.fixture
def rawconn():
    """A connection with no teardown invariant check.

    For tests that deliberately corrupt the database to prove the invariant
    checker fires. The `conn` fixture would fail such a test at teardown for
    the very corruption it was demonstrating.
    """
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    schema.migrate(c)
    yield c
    c.close()


@pytest.fixture
def rawbook(rawconn):
    g = ledger.create_category_group(rawconn, "Everyday")
    return {
        "conn": rawconn,
        "checking": ledger.create_account(rawconn, "Checking"),
        "savings": ledger.create_account(rawconn, "Savings"),
        "groceries": ledger.create_category(rawconn, g, "Groceries"),
        "fuel": ledger.create_category(rawconn, g, "Fuel"),
    }
