"""Edges: off-budget accounts, deletion, moving between months, carryover chains."""

import pytest

from dashboard import ledger


# ── off-budget accounts ───────────────────────────────────────────────────
def test_off_budget_spending_does_not_touch_the_envelope(conn, book):
    """A tracking account is outside the budget by definition.

    Investment and loan accounts are recorded so net worth is right, not so
    their movements consume this month's grocery money.
    """
    tracking = ledger.create_account(conn, "Brokerage", type="investment",
                                     on_budget=False)
    ledger.set_budget(conn, "2026-01", book["groceries"], 100_00)
    ledger.add_transaction(conn, tracking, "2026-01-05", -80_00,
                           "Fees", book["groceries"])
    assert ledger.category_activity(conn, book["groceries"], "2026-01") == 0
    assert ledger.category_balance(conn, book["groceries"], "2026-01") == 100_00


def test_off_budget_balance_is_still_tracked(conn, book):
    tracking = ledger.create_account(conn, "Brokerage", on_budget=False)
    ledger.add_transaction(conn, tracking, "2026-01-05", 5000_00, "Deposit")
    assert ledger.account_balance(conn, tracking) == 5000_00


def test_off_budget_income_is_not_available_to_budget(conn, book):
    tracking = ledger.create_account(conn, "Brokerage", on_budget=False)
    ledger.add_transaction(conn, tracking, "2026-01-05", 900_00,
                           "Dividend", book["salary"])
    assert ledger.to_be_budgeted(conn, "2026-01") == 0


# ── deletion ──────────────────────────────────────────────────────────────
def test_deleted_transactions_vanish_from_every_calculation(conn, book):
    ledger.add_transaction(conn, book["checking"], "2026-01-01", 500_00,
                           "Salary", book["salary"])
    ledger.set_budget(conn, "2026-01", book["groceries"], 100_00)
    t = ledger.add_transaction(conn, book["checking"], "2026-01-09", -40_00,
                               "Shop", book["groceries"])
    ledger.delete_transaction(conn, t)
    assert ledger.account_balance(conn, book["checking"]) == 500_00
    assert ledger.category_activity(conn, book["groceries"], "2026-01") == 0
    assert ledger.category_balance(conn, book["groceries"], "2026-01") == 100_00


def test_deleting_an_already_deleted_row_is_a_no_op(conn, book):
    t = ledger.add_transaction(conn, book["checking"], "2026-01-09", -40_00)
    assert ledger.delete_transaction(conn, t) == 1
    assert ledger.delete_transaction(conn, t) == 0


def test_deleting_an_unknown_id_is_a_no_op(conn, book):
    assert ledger.delete_transaction(conn, "does-not-exist") == 0


# ── editing ───────────────────────────────────────────────────────────────
def test_changing_the_date_moves_it_to_another_month(conn, book):
    t = ledger.add_transaction(conn, book["checking"], "2026-01-31", -25_00,
                               "Shop", book["groceries"])
    assert ledger.category_activity(conn, book["groceries"], "2026-01") == -25_00
    ledger.update_transaction(conn, t, date="2026-02-01")
    assert ledger.category_activity(conn, book["groceries"], "2026-01") == 0
    assert ledger.category_activity(conn, book["groceries"], "2026-02") == -25_00


def test_recategorising_moves_the_activity(conn, book):
    t = ledger.add_transaction(conn, book["checking"], "2026-01-09", -30_00,
                               "Shop", book["groceries"])
    ledger.update_transaction(conn, t, category_id=book["fuel"])
    assert ledger.category_activity(conn, book["groceries"], "2026-01") == 0
    assert ledger.category_activity(conn, book["fuel"], "2026-01") == -30_00


def test_unknown_fields_are_refused(conn, book):
    t = ledger.add_transaction(conn, book["checking"], "2026-01-09", -30_00)
    with pytest.raises(ValueError):
        ledger.update_transaction(conn, t, account_id=book["savings"])
    with pytest.raises(ValueError):
        ledger.update_transaction(conn, t, deleted=1)


def test_editing_a_missing_transaction_raises(conn, book):
    with pytest.raises(KeyError):
        ledger.update_transaction(conn, "nope", notes="x")


def test_payee_norm_follows_the_payee(conn, book):
    t = ledger.add_transaction(conn, book["checking"], "2026-01-09", -30_00,
                               payee="  TESCO   STORES ")
    row = conn.execute("SELECT payee_norm FROM transactions WHERE id=?",
                       (t,)).fetchone()
    assert row["payee_norm"] == "tesco stores"
    ledger.update_transaction(conn, t, payee="Sainsbury's")
    row = conn.execute("SELECT payee_norm FROM transactions WHERE id=?",
                       (t,)).fetchone()
    assert row["payee_norm"] == "sainsbury's"


# ── carryover over several months ─────────────────────────────────────────
def test_carryover_accumulates_across_a_run_of_months(conn, book):
    for m in ("2026-01", "2026-02", "2026-03"):
        ledger.set_budget(conn, m, book["groceries"], 100_00)
    ledger.add_transaction(conn, book["checking"], "2026-01-09", -60_00,
                           "Shop", book["groceries"])
    ledger.add_transaction(conn, book["checking"], "2026-02-09", -90_00,
                           "Shop", book["groceries"])
    # Jan leaves 40, Feb adds 100 and spends 90 -> 50, Mar adds 100 -> 150
    assert ledger.category_balance(conn, book["groceries"], "2026-01") == 40_00
    assert ledger.category_balance(conn, book["groceries"], "2026-02") == 50_00
    assert ledger.category_balance(conn, book["groceries"], "2026-03") == 150_00


def test_an_untouched_month_reports_zero(conn, book):
    assert ledger.category_balance(conn, book["groceries"], "2029-12") == 0
    assert ledger.to_be_budgeted(conn, "2029-12") == 0


def test_carried_debt_can_be_repaid_by_a_later_budget(conn, book):
    ledger.set_budget(conn, "2026-01", book["rent"], 100_00)
    ledger.add_transaction(conn, book["checking"], "2026-01-05", -160_00,
                           "Landlord", book["rent"])
    assert ledger.category_balance(conn, book["rent"], "2026-01") == -60_00
    ledger.set_budget(conn, "2026-02", book["rent"], 160_00)
    assert ledger.category_balance(conn, book["rent"], "2026-02") == 100_00


# ── budgeting operations ──────────────────────────────────────────────────
def test_setting_a_budget_twice_replaces_rather_than_accumulates(conn, book):
    ledger.set_budget(conn, "2026-01", book["groceries"], 100_00)
    ledger.set_budget(conn, "2026-01", book["groceries"], 250_00)
    assert ledger.get_budget(conn, "2026-01", book["groceries"]) == 250_00


def test_budget_must_be_integer_cents(conn, book):
    with pytest.raises(TypeError):
        ledger.set_budget(conn, "2026-01", book["groceries"], 100.5)


def test_moving_more_than_is_there_is_allowed_and_shows_as_negative(conn, book):
    # Deliberately permitted: the UI should be able to represent a mistake
    # rather than silently refusing a drag, and the number tells the truth.
    ledger.set_budget(conn, "2026-01", book["groceries"], 10_00)
    ledger.move_money(conn, "2026-01", book["groceries"], book["fuel"], 50_00)
    assert ledger.get_budget(conn, "2026-01", book["groceries"]) == -40_00
    assert ledger.get_budget(conn, "2026-01", book["fuel"]) == 50_00


def test_move_money_rejects_nonsense(conn, book):
    with pytest.raises(ValueError):
        ledger.move_money(conn, "2026-01", book["groceries"], book["fuel"], -5)
    with pytest.raises(ValueError):
        ledger.move_money(conn, "2026-01", book["groceries"], book["groceries"], 5)


# ── audit ─────────────────────────────────────────────────────────────────
def test_every_money_operation_is_audited(conn, book):
    ledger.add_transaction(conn, book["checking"], "2026-01-09", -30_00)
    ledger.add_transfer(conn, book["checking"], book["savings"], "2026-01-10", 10_00)
    ledger.set_budget(conn, "2026-01", book["groceries"], 100_00)
    actions = [r["action"] for r in
               conn.execute("SELECT action FROM audit_log ORDER BY id")]
    assert "create" in actions and "budget" in actions
    assert len(actions) >= 4


def test_audit_records_no_payee_or_note_text(conn, book):
    ledger.add_transaction(conn, book["checking"], "2026-01-09", -30_00,
                           payee="Dr Smith Psychiatry", notes="therapy")
    detail = " ".join(r["detail"] for r in
                      conn.execute("SELECT detail FROM audit_log"))
    # The audit log records that something changed, never the sensitive value.
    assert "Psychiatry" not in detail and "therapy" not in detail


# ── splits and transfers move as one unit ─────────────────────────────────
def test_moving_a_split_takes_its_parts_with_it(conn, book):
    p = ledger.add_split(conn, book["checking"], "2026-01-31",
                         [(book["groceries"], -50_00), (book["fuel"], -30_00)])
    assert ledger.category_activity(conn, book["groceries"], "2026-01") == -50_00
    ledger.update_transaction(conn, p, date="2026-02-01")
    # Money and categorisation must land in the same month, or the envelope
    # reports spending in a month the money never left.
    assert ledger.category_activity(conn, book["groceries"], "2026-01") == 0
    assert ledger.category_activity(conn, book["groceries"], "2026-02") == -50_00
    assert ledger.account_balance(conn, book["checking"], as_of="2026-01-31") == 0


def test_a_split_part_cannot_be_moved_on_its_own(conn, book):
    p = ledger.add_split(conn, book["checking"], "2026-01-12",
                         [(book["groceries"], -50_00), (book["fuel"], -30_00)])
    child = conn.execute("SELECT id FROM transactions WHERE parent_id=?",
                         (p,)).fetchone()["id"]
    with pytest.raises(ValueError):
        ledger.update_transaction(conn, child, date="2026-03-01")


def test_moving_a_transfer_moves_both_halves(conn, book):
    out, inn = ledger.add_transfer(conn, book["checking"], book["savings"],
                                   "2026-01-31", 100_00)
    ledger.update_transaction(conn, out, date="2026-02-02")
    dates = {r["date"] for r in conn.execute(
        "SELECT date FROM transactions WHERE id IN (?,?)", (out, inn))}
    assert dates == {"2026-02-02"}, "a transfer is one movement on one day"


# ── guard clauses ─────────────────────────────────────────────────────────
def test_a_split_needs_parts(conn, book):
    with pytest.raises(ValueError):
        ledger.add_split(conn, book["checking"], "2026-01-12", [])


def test_split_amounts_must_be_integer_cents(conn, book):
    with pytest.raises(TypeError):
        ledger.add_split(conn, book["checking"], "2026-01-12",
                         [(book["groceries"], -50.5)])


def test_editing_to_a_float_amount_is_refused(conn, book):
    t = ledger.add_transaction(conn, book["checking"], "2026-01-09", -3000)
    with pytest.raises(TypeError):
        ledger.update_transaction(conn, t, amount_cents=-30.5)


def test_balance_of_an_unknown_category_raises(conn, book):
    with pytest.raises(KeyError):
        ledger.category_balance(conn, "no-such-category", "2026-01")
