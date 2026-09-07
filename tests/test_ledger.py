"""Invariant tests for the ledger.

These target the failures that do not announce themselves: money that is
quietly doubled, halved, duplicated or invented. Every test also runs
check_invariants() at teardown via the conn fixture.
"""

import pytest

from dashboard import ledger
from dashboard.money import to_cents


# ── balances ──────────────────────────────────────────────────────────────
def test_balance_is_sum_of_transactions(conn, book):
    ledger.add_transaction(conn, book["checking"], "2026-01-05", -1234,
                           "Shop", book["groceries"])
    ledger.add_transaction(conn, book["checking"], "2026-01-06", -500,
                           "Fuel", book["fuel"])
    ledger.add_transaction(conn, book["checking"], "2026-01-07", 250_00,
                           "Salary", book["salary"])
    assert ledger.account_balance(conn, book["checking"]) == -1234 - 500 + 25000


def test_as_of_excludes_later_transactions(conn, book):
    ledger.add_transaction(conn, book["checking"], "2026-01-05", -100)
    ledger.add_transaction(conn, book["checking"], "2026-02-05", -100)
    assert ledger.account_balance(conn, book["checking"], as_of="2026-01-31") == -100


def test_float_amounts_are_refused(conn, book):
    # A float here is how fractions of a cent get silently introduced.
    with pytest.raises(TypeError):
        ledger.add_transaction(conn, book["checking"], "2026-01-05", -12.34)


# ── transfers ─────────────────────────────────────────────────────────────
def test_transfer_moves_money_and_nets_to_zero(conn, book):
    ledger.add_transaction(conn, book["checking"], "2026-01-01", 100_00,
                           "Opening", book["salary"])
    ledger.add_transfer(conn, book["checking"], book["savings"],
                        "2026-01-10", 30_00)
    assert ledger.account_balance(conn, book["checking"]) == 70_00
    assert ledger.account_balance(conn, book["savings"]) == 30_00
    total = (ledger.account_balance(conn, book["checking"])
             + ledger.account_balance(conn, book["savings"]))
    assert total == 100_00, "a transfer must not change total net worth"


def test_transfer_is_never_categorised(conn, book):
    out, _ = ledger.add_transfer(conn, book["checking"], book["savings"],
                                 "2026-01-10", 5_00)
    with pytest.raises(ValueError):
        ledger.update_transaction(conn, out, category_id=book["groceries"])


def test_transfer_does_not_count_as_spending(conn, book):
    ledger.add_transfer(conn, book["checking"], book["savings"],
                        "2026-01-10", 40_00)
    # The classic bug: a transfer appearing as expenditure inflates spending.
    assert ledger.category_activity(conn, book["groceries"], "2026-01") == 0


def test_transfer_rejects_nonsense(conn, book):
    with pytest.raises(ValueError):
        ledger.add_transfer(conn, book["checking"], book["checking"],
                            "2026-01-10", 100)
    with pytest.raises(ValueError):
        ledger.add_transfer(conn, book["checking"], book["savings"],
                            "2026-01-10", -100)


def test_editing_one_half_of_a_transfer_moves_both(conn, book):
    out, inn = ledger.add_transfer(conn, book["checking"], book["savings"],
                                   "2026-01-10", 20_00)
    ledger.update_transaction(conn, out, amount_cents=-25_00)
    assert ledger.account_balance(conn, book["checking"]) == -25_00
    assert ledger.account_balance(conn, book["savings"]) == 25_00


def test_deleting_one_half_deletes_both(conn, book):
    out, inn = ledger.add_transfer(conn, book["checking"], book["savings"],
                                   "2026-01-10", 20_00)
    assert ledger.delete_transaction(conn, out) == 2
    assert ledger.account_balance(conn, book["checking"]) == 0
    assert ledger.account_balance(conn, book["savings"]) == 0


# ── splits ────────────────────────────────────────────────────────────────
def test_split_counts_once_in_balance(conn, book):
    ledger.add_split(conn, book["checking"], "2026-01-12",
                     [(book["groceries"], -60_00), (book["fuel"], -40_00)],
                     payee="Supermarket")
    # -100.00, not -200.00. Counting parent and children both is the bug.
    assert ledger.account_balance(conn, book["checking"]) == -100_00


def test_split_children_carry_the_categories(conn, book):
    ledger.add_split(conn, book["checking"], "2026-01-12",
                     [(book["groceries"], -60_00), (book["fuel"], -40_00)])
    assert ledger.category_activity(conn, book["groceries"], "2026-01") == -60_00
    assert ledger.category_activity(conn, book["fuel"], "2026-01") == -40_00


def test_split_parent_total_cannot_be_edited_directly(conn, book):
    p = ledger.add_split(conn, book["checking"], "2026-01-12",
                         [(book["groceries"], -60_00), (book["fuel"], -40_00)])
    with pytest.raises(ValueError):
        ledger.update_transaction(conn, p, amount_cents=-999_00)


def test_deleting_a_split_child_removes_the_whole_split(conn, book):
    p = ledger.add_split(conn, book["checking"], "2026-01-12",
                         [(book["groceries"], -60_00), (book["fuel"], -40_00)])
    child = conn.execute("SELECT id FROM transactions WHERE parent_id=?",
                         (p,)).fetchone()["id"]
    ledger.delete_transaction(conn, child)
    assert ledger.account_balance(conn, book["checking"]) == 0
    assert ledger.category_activity(conn, book["groceries"], "2026-01") == 0


# ── import idempotency ────────────────────────────────────────────────────
def test_same_imported_id_cannot_land_twice(conn, book):
    import sqlite3
    ledger.add_transaction(conn, book["checking"], "2026-01-05", -1000,
                           "Shop", imported_id="abc123", source="import")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.add_transaction(conn, book["checking"], "2026-01-05", -1000,
                               "Shop", imported_id="abc123", source="import")
    conn.rollback()
    assert ledger.account_balance(conn, book["checking"]) == -1000


def test_same_imported_id_in_a_different_account_is_fine(conn, book):
    ledger.add_transaction(conn, book["checking"], "2026-01-05", -1000,
                           imported_id="row-1", source="import")
    ledger.add_transaction(conn, book["savings"], "2026-01-05", -1000,
                           imported_id="row-1", source="import")
    assert ledger.account_balance(conn, book["savings"]) == -1000


# ── envelope budgeting ────────────────────────────────────────────────────
def test_category_balance_is_budgeted_plus_activity(conn, book):
    ledger.set_budget(conn, "2026-01", book["groceries"], 400_00)
    ledger.add_transaction(conn, book["checking"], "2026-01-09", -120_00,
                           "Shop", book["groceries"])
    assert ledger.category_balance(conn, book["groceries"], "2026-01") == 280_00


def test_positive_balance_carries_forward(conn, book):
    ledger.set_budget(conn, "2026-01", book["groceries"], 100_00)
    ledger.add_transaction(conn, book["checking"], "2026-01-09", -30_00,
                           "Shop", book["groceries"])
    ledger.set_budget(conn, "2026-02", book["groceries"], 100_00)
    # 70 left over plus 100 newly budgeted
    assert ledger.category_balance(conn, book["groceries"], "2026-02") == 170_00


def test_overspend_is_absorbed_by_default(conn, book):
    ledger.set_budget(conn, "2026-01", book["groceries"], 50_00)
    ledger.add_transaction(conn, book["checking"], "2026-01-09", -80_00,
                           "Shop", book["groceries"])
    assert ledger.category_balance(conn, book["groceries"], "2026-01") == -30_00
    ledger.set_budget(conn, "2026-02", book["groceries"], 50_00)
    # The 30 overspend does not haunt the envelope; it was settled.
    assert ledger.category_balance(conn, book["groceries"], "2026-02") == 50_00


def test_overspend_carries_when_the_category_asks_it_to(conn, book):
    ledger.set_budget(conn, "2026-01", book["rent"], 50_00)
    ledger.add_transaction(conn, book["checking"], "2026-01-09", -80_00,
                           "Landlord", book["rent"])
    ledger.set_budget(conn, "2026-02", book["rent"], 50_00)
    # rent has carryover_negative=True, so the debt follows it
    assert ledger.category_balance(conn, book["rent"], "2026-02") == 20_00


def test_to_be_budgeted_tracks_income_minus_assignments(conn, book):
    ledger.add_transaction(conn, book["checking"], "2026-01-01", 1000_00,
                           "Salary", book["salary"])
    assert ledger.to_be_budgeted(conn, "2026-01") == 1000_00
    ledger.set_budget(conn, "2026-01", book["groceries"], 400_00)
    assert ledger.to_be_budgeted(conn, "2026-01") == 600_00


def test_overspend_reduces_to_be_budgeted(conn, book):
    ledger.add_transaction(conn, book["checking"], "2026-01-01", 1000_00,
                           "Salary", book["salary"])
    ledger.set_budget(conn, "2026-01", book["groceries"], 100_00)
    ledger.add_transaction(conn, book["checking"], "2026-01-09", -150_00,
                           "Shop", book["groceries"])
    # 1000 income - 100 assigned - 50 overspent that had to come from somewhere
    assert ledger.to_be_budgeted(conn, "2026-01") == 850_00


def test_moving_money_preserves_the_months_total(conn, book):
    ledger.set_budget(conn, "2026-01", book["groceries"], 300_00)
    ledger.set_budget(conn, "2026-01", book["fuel"], 100_00)
    before = (ledger.get_budget(conn, "2026-01", book["groceries"])
              + ledger.get_budget(conn, "2026-01", book["fuel"]))
    ledger.move_money(conn, "2026-01", book["groceries"], book["fuel"], 50_00)
    after = (ledger.get_budget(conn, "2026-01", book["groceries"])
             + ledger.get_budget(conn, "2026-01", book["fuel"]))
    assert before == after == 400_00
    assert ledger.get_budget(conn, "2026-01", book["fuel"]) == 150_00


def test_transfers_do_not_create_income(conn, book):
    ledger.add_transaction(conn, book["checking"], "2026-01-01", 500_00,
                           "Salary", book["salary"])
    ledger.add_transfer(conn, book["checking"], book["savings"],
                        "2026-01-15", 200_00)
    # Moving money to savings must not look like earning it again.
    assert ledger.to_be_budgeted(conn, "2026-01") == 500_00


# ── reconciliation ────────────────────────────────────────────────────────
def test_reconciled_rows_are_frozen(conn, book):
    t = ledger.add_transaction(conn, book["checking"], "2026-01-05", -1000)
    ledger.update_transaction(conn, t, reconciled=True)
    with pytest.raises(ValueError):
        ledger.update_transaction(conn, t, amount_cents=-2000)
    ledger.update_transaction(conn, t, notes="fine to edit")


# ── the whole picture ─────────────────────────────────────────────────────
def test_realistic_month_stays_consistent(conn, book):
    ledger.add_transaction(conn, book["checking"], "2026-01-01", 3000_00,
                           "Salary", book["salary"])
    ledger.set_budget(conn, "2026-01", book["groceries"], 400_00)
    ledger.set_budget(conn, "2026-01", book["fuel"], 150_00)
    ledger.set_budget(conn, "2026-01", book["rent"], 1200_00)
    ledger.add_transaction(conn, book["checking"], "2026-01-02", -1200_00,
                           "Landlord", book["rent"])
    ledger.add_split(conn, book["checking"], "2026-01-08",
                     [(book["groceries"], -85_50), (book["fuel"], -44_50)],
                     payee="Superstore")
    ledger.add_transfer(conn, book["checking"], book["savings"],
                        "2026-01-20", 500_00)
    ledger.add_transaction(conn, book["checking"], "2026-01-25", -to_cents("62.30"),
                           "Market", book["groceries"])

    assert ledger.account_balance(conn, book["checking"]) == \
        3000_00 - 1200_00 - 130_00 - 500_00 - 6230
    assert ledger.account_balance(conn, book["savings"]) == 500_00
    assert ledger.category_balance(conn, book["groceries"], "2026-01") == \
        400_00 - 85_50 - 6230
    assert ledger.category_balance(conn, book["rent"], "2026-01") == 0
    assert ledger.to_be_budgeted(conn, "2026-01") == 3000_00 - 1750_00
    assert ledger.check_invariants(conn) == []


# ── splitting one payment across categories ───────────────────────────────
def test_a_payment_can_be_split_across_categories(conn, book):
    """Fuel and a sandwich bought together are one payment and two things."""
    txn = ledger.add_transaction(conn, book["checking"], "2026-08-01", -5000,
                                 "SHELL 4471", book["fuel"])
    ledger.split_transaction(conn, txn, [
        (book["fuel"], -4200), (book["groceries"], -800)])
    parts = ledger.split_parts(conn, txn)
    assert [p["amount_cents"] for p in parts] == [-4200, -800]
    assert sum(p["amount_cents"] for p in parts) == -5000


def test_the_parent_keeps_its_id(conn, book):
    """The id carries the bank's FITID. Losing it would make the same
    statement re-import as new spending."""
    txn = ledger.add_transaction(conn, book["checking"], "2026-08-01", -5000,
                                 "SHELL 4471", book["fuel"])
    same = ledger.split_transaction(conn, txn, [
        (book["fuel"], -4200), (book["groceries"], -800)])
    assert same == txn
    assert conn.execute("SELECT 1 FROM transactions WHERE id=?",
                        (txn,)).fetchone() is not None


def test_the_parent_loses_its_category(conn, book):
    """The children hold the meaning now; a categorised parent would count
    the money twice."""
    txn = ledger.add_transaction(conn, book["checking"], "2026-08-01", -5000,
                                 "SHELL 4471", book["fuel"])
    ledger.split_transaction(conn, txn, [
        (book["fuel"], -4200), (book["groceries"], -800)])
    assert conn.execute("SELECT category_id FROM transactions WHERE id=?",
                        (txn,)).fetchone()[0] is None


def test_the_balance_does_not_move(conn, book):
    before = ledger.account_balance(conn, book["checking"])
    txn = ledger.add_transaction(conn, book["checking"], "2026-08-01", -5000,
                                 "SHELL 4471", book["fuel"])
    ledger.split_transaction(conn, txn, [
        (book["fuel"], -4200), (book["groceries"], -800)])
    assert ledger.account_balance(conn, book["checking"]) == before - 5000


def test_parts_that_do_not_add_up_are_refused(conn, book):
    """A split says what a payment was for, not how much it was. A balance
    that moves while someone itemises a receipt is found months later."""
    txn = ledger.add_transaction(conn, book["checking"], "2026-08-01", -5000,
                                 "SHELL 4471", book["fuel"])
    with pytest.raises(ValueError, match="add up"):
        ledger.split_transaction(conn, txn, [
            (book["fuel"], -4200), (book["groceries"], -900)])


def test_re_splitting_replaces_rather_than_accumulates(conn, book):
    txn = ledger.add_transaction(conn, book["checking"], "2026-08-01", -5000,
                                 "SHELL 4471", book["fuel"])
    ledger.split_transaction(conn, txn, [
        (book["fuel"], -4200), (book["groceries"], -800)])
    ledger.split_transaction(conn, txn, [
        (book["fuel"], -3000), (book["groceries"], -2000)])
    parts = ledger.split_parts(conn, txn)
    assert len(parts) == 2
    assert sum(p["amount_cents"] for p in parts) == -5000


def test_a_transfer_cannot_be_split(conn, book):
    """It moves money between your own accounts, so there is nothing to
    divide across categories."""
    ledger.add_transfer(conn, book["checking"], book["savings"],
                        "2026-08-01", 10000)
    txn = conn.execute("SELECT id FROM transactions WHERE transfer_id IS NOT NULL"
                       " LIMIT 1").fetchone()[0]
    with pytest.raises(ValueError, match="transfer"):
        ledger.split_transaction(conn, txn, [(book["fuel"], 10000)])


def test_a_split_child_cannot_itself_be_split(conn, book):
    txn = ledger.add_transaction(conn, book["checking"], "2026-08-01", -5000,
                                 "SHELL 4471", book["fuel"])
    ledger.split_transaction(conn, txn, [
        (book["fuel"], -4200), (book["groceries"], -800)])
    child = ledger.split_parts(conn, txn)[0]["id"]
    with pytest.raises(ValueError, match="already part of a split"):
        ledger.split_transaction(conn, child, [(book["fuel"], -4200)])


def test_unsplitting_puts_it_back(conn, book):
    txn = ledger.add_transaction(conn, book["checking"], "2026-08-01", -5000,
                                 "SHELL 4471", book["fuel"])
    ledger.split_transaction(conn, txn, [
        (book["fuel"], -4200), (book["groceries"], -800)])
    assert ledger.unsplit_transaction(conn, txn) == 2
    assert ledger.split_parts(conn, txn) == []
    assert ledger.account_balance(conn, book["checking"]) == -5000


def test_an_unknown_category_is_refused(conn, book):
    txn = ledger.add_transaction(conn, book["checking"], "2026-08-01", -5000,
                                 "SHELL 4471", book["fuel"])
    with pytest.raises(KeyError):
        ledger.split_transaction(conn, txn, [("0" * 32, -5000)])
