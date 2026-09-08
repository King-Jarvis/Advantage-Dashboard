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
        ledger.update_transaction(conn, t, deleted=1)


def test_an_ordinary_row_can_move_account(conn, book):
    """A statement filed against the wrong account is the reason this is
    editable. It used to be refused outright."""
    t = ledger.add_transaction(conn, book["checking"], "2026-01-09", -30_00)
    ledger.update_transaction(conn, t, account_id=book["savings"])
    assert ledger.account_balance(conn, book["checking"]) == 0
    assert ledger.account_balance(conn, book["savings"]) == -30_00


def test_moving_an_unknown_account_is_refused(conn, book):
    t = ledger.add_transaction(conn, book["checking"], "2026-01-09", -30_00)
    with pytest.raises(KeyError):
        ledger.update_transaction(conn, t, account_id="0" * 32)


def test_half_a_transfer_cannot_move_alone(conn, book):
    """Both halves would end up in one account, and the movement would stop
    being a movement."""
    ledger.add_transfer(conn, book["checking"], book["savings"],
                        "2026-01-09", 100_00)
    half = conn.execute("SELECT id FROM transactions"
                        " WHERE transfer_id IS NOT NULL LIMIT 1").fetchone()[0]
    with pytest.raises(ValueError, match="unlink"):
        ledger.update_transaction(conn, half, account_id=book["savings"])


def test_a_split_part_cannot_change_account_on_its_own(conn, book):
    t = ledger.add_transaction(conn, book["checking"], "2026-01-09", -50_00)
    ledger.split_transaction(conn, t, [(book["groceries"], -30_00),
                                       (book["fuel"], -20_00)])
    part = ledger.split_parts(conn, t)[0]["id"]
    with pytest.raises(ValueError, match="whole split"):
        ledger.update_transaction(conn, part, account_id=book["savings"])


def test_moving_a_split_to_another_account_takes_its_parts(conn, book):
    """A split spanning two accounts stops adding up in both."""
    t = ledger.add_transaction(conn, book["checking"], "2026-01-09", -50_00)
    ledger.split_transaction(conn, t, [(book["groceries"], -30_00),
                                       (book["fuel"], -20_00)])
    ledger.update_transaction(conn, t, account_id=book["savings"])
    accounts = {r[0] for r in conn.execute(
        "SELECT account_id FROM transactions WHERE deleted=0")}
    assert accounts == {book["savings"]}


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


# ── deleting a category ───────────────────────────────────────────────────
def test_unused_category_is_deleted_outright(conn, book):
    cid = ledger.create_category(conn, book_group(conn), "Scratch")
    assert ledger.delete_category(conn, cid) == "deleted"
    assert not any(c["id"] == cid
                   for c in ledger.list_categories(conn, include_hidden=True))


def test_category_with_live_spending_is_hidden_not_deleted(conn, book):
    ledger.add_transaction(conn, book["checking"], "2026-01-05", -20_00,
                           "Shop", book["fuel"])
    assert ledger.delete_category(conn, book["fuel"]) == "hidden"
    kept = [c for c in ledger.list_categories(conn, include_hidden=True)
            if c["id"] == book["fuel"]]
    assert kept and kept[0]["hidden"] == 1


def test_soft_deleted_transactions_do_not_block_deleting_a_category(conn, book):
    """A row that is already gone from every view must not pin the category.

    Nothing reads a deleted transaction's category, so keeping the reference
    protects no history -- it only makes the foreign key refuse the delete,
    and the screen cannot explain why.
    """
    tid = ledger.add_transaction(conn, book["checking"], "2026-01-05", -20_00,
                                 "Shop", book["fuel"])
    ledger.delete_transaction(conn, tid)
    assert ledger.delete_category(conn, book["fuel"]) == "deleted"
    assert conn.execute("SELECT category_id FROM transactions WHERE id=?",
                        (tid,)).fetchone()["category_id"] is None


def test_import_staging_rows_do_not_block_deleting_a_category(conn, book):
    conn.execute("INSERT INTO import_batches (id, account_id, filename,"
                 " uploaded_at) VALUES ('b1',?,'stmt.qfx','2026-01-01T00:00:00Z')",
                 (book["checking"],))
    conn.execute(
        "INSERT INTO import_rows (id, batch_id, line_no, date, amount_cents,"
        " payee, category_id) VALUES ('r1','b1',0,'2026-01-05',-2000,'Shop',?)",
        (book["fuel"],))
    conn.commit()
    assert ledger.delete_category(conn, book["fuel"]) == "deleted"
    assert conn.execute("SELECT category_id FROM import_rows WHERE id='r1'"
                        ).fetchone()["category_id"] is None


def test_zeroing_a_budget_lets_a_spent_out_category_go(conn, book):
    """Budgeting to a category should not make it permanent once emptied."""
    ledger.set_budget(conn, "2026-01", book["fuel"], 50_00)
    assert ledger.delete_category(conn, book["fuel"]) == "hidden"
    ledger.update_category(conn, book["fuel"], hidden=False)
    ledger.set_budget(conn, "2026-01", book["fuel"], 0)
    assert ledger.delete_category(conn, book["fuel"]) == "deleted"
    assert conn.execute("SELECT COUNT(*) c FROM budget_months WHERE"
                        " category_id=?", (book["fuel"],)).fetchone()["c"] == 0


def book_group(conn):
    return conn.execute("SELECT id FROM category_groups LIMIT 1").fetchone()["id"]


# ── scanning for transfers that did not pair ──────────────────────────────
def test_a_pair_outside_the_window_is_a_near_miss_not_a_candidate(conn, book):
    """The strict scan must not widen; the wider one must find it.

    Two equal and opposite amounts eleven days apart could be a transfer that
    cleared slowly, or two unrelated things. The answer is to show it and say
    how far apart it is, not to guess.
    """
    out = ledger.add_transaction(conn, book["checking"], "2026-01-02",
                                 -50_00, "To savings")
    inn = ledger.add_transaction(conn, book["savings"], "2026-01-13",
                                 50_00, "From checking")
    assert ledger.transfer_candidates(conn) == []
    near = ledger.near_misses(conn)
    assert len(near) == 1
    assert near[0]["days_apart"] == 11
    assert {near[0]["out_id"], near[0]["in_id"]} == {out, inn}


def test_a_near_miss_can_be_linked_once_you_recognise_it(conn, book):
    ledger.add_transaction(conn, book["checking"], "2026-01-02", -50_00, "Out")
    ledger.add_transaction(conn, book["savings"], "2026-01-13", 50_00, "In")
    near = ledger.near_misses(conn)[0]
    ledger.link_transfer(conn, near["out_id"], near["in_id"])
    assert ledger.near_misses(conn) == []
    assert len(ledger.transfers(conn)) == 1


def test_the_wider_scan_still_refuses_one_account(conn, book):
    """A refund cancelling a charge is not a movement, at any distance."""
    ledger.add_transaction(conn, book["checking"], "2026-01-02", -16_23,
                           "Amazon")
    ledger.add_transaction(conn, book["checking"], "2026-03-20", 16_23,
                           "Amazon refund")
    assert ledger.near_misses(conn) == []


def test_the_wider_scan_stops_somewhere(conn, book):
    ledger.add_transaction(conn, book["checking"], "2026-01-02", -50_00, "Out")
    ledger.add_transaction(conn, book["savings"], "2026-11-13", 50_00, "In")
    assert ledger.near_misses(conn, window_days=45) == []
    assert len(ledger.near_misses(conn, window_days=400)) == 1


def test_a_movement_with_no_other_half_is_named_as_such(conn, book):
    """Money to Apple Pay will never pair, and saying nothing implies it may."""
    ledger.add_transaction(conn, book["checking"], "2026-01-02", -35_00,
                           "Transfer to Apple Pay")
    assert ledger.transfer_candidates(conn) == []
    assert ledger.near_misses(conn) == []
    lone = ledger.unpaired_movements(conn)
    assert [r["payee"] for r in lone] == ["Transfer to Apple Pay"]


def test_something_that_could_still_pair_is_not_called_unpairable(conn, book):
    ledger.add_transaction(conn, book["checking"], "2026-01-02", -35_00,
                           "Transfer to Apple Pay")
    ledger.add_transaction(conn, book["savings"], "2026-02-20", 35_00,
                           "Transfer from Apple Pay")
    assert ledger.unpaired_movements(conn) == []


def test_ordinary_spending_is_not_a_movement(conn, book):
    ledger.add_transaction(conn, book["checking"], "2026-01-02", -35_00,
                           "Tesco Stores")
    assert ledger.unpaired_movements(conn) == []


def test_an_off_budget_account_can_receive_what_cannot_pair(conn, book):
    wallet = ledger.create_account(conn, "Apple Pay", type="wallet",
                                   on_budget=False)
    tid = ledger.add_transaction(conn, book["checking"], "2026-01-02", -35_00,
                                 "Transfer to Apple Pay")
    ledger.send_to_account(conn, tid, wallet)
    assert ledger.unpaired_movements(conn) == []
    assert len(ledger.transfers(conn)) == 1


# ── what the scan must not try to pair ────────────────────────────────────
TERMS = ["apple pay", "venmo", "loan"]


def test_a_wallet_payment_is_never_offered_as_a_candidate(conn, book):
    """Money to Apple Pay has its other half in no statement you will import.

    Two equal and opposite amounts a few days apart is then a coincidence
    wearing the shape of a transfer, and proposing it is worse than silence:
    accepting it invents a movement and hides a real expense.
    """
    ledger.add_transaction(conn, book["checking"], "2026-01-02", -50_00,
                           "Transfer to Apple Pay")
    ledger.add_transaction(conn, book["savings"], "2026-01-03", 50_00,
                           "Deposit Internet Banking Transfer")
    assert len(ledger.transfer_candidates(conn)) == 1
    assert ledger.transfer_candidates(conn, exclude=TERMS) == []


def test_the_exclusion_applies_to_the_incoming_half_too(conn, book):
    ledger.add_transaction(conn, book["checking"], "2026-01-02", -50_00,
                           "Withdrawal Internet Banking Transfer")
    ledger.add_transaction(conn, book["savings"], "2026-01-03", 50_00,
                           "Transfer from Venmo")
    assert ledger.transfer_candidates(conn, exclude=TERMS) == []


def test_a_real_account_transfer_still_pairs(conn, book):
    ledger.add_transaction(conn, book["checking"], "2026-01-02", -50_00,
                           "Transfer To Share 0000")
    ledger.add_transaction(conn, book["savings"], "2026-01-03", 50_00,
                           "Transfer From Share 0000")
    assert len(ledger.transfer_candidates(conn, exclude=TERMS)) == 1


def test_the_wider_scan_honours_the_same_exclusions(conn, book):
    ledger.add_transaction(conn, book["checking"], "2026-01-02", -50_00,
                           "Transfer to Apple Pay")
    ledger.add_transaction(conn, book["savings"], "2026-01-20", 50_00,
                           "Deposit Internet Banking Transfer")
    assert len(ledger.near_misses(conn)) == 1
    assert ledger.near_misses(conn, exclude=TERMS) == []


def test_excluded_rows_are_reported_not_hidden(conn, book):
    """A scan that silently skips things is one you stop trusting."""
    ledger.add_transaction(conn, book["checking"], "2026-01-02", -35_00,
                           "Transfer to Apple Pay")
    ledger.add_transaction(conn, book["checking"], "2026-01-05", -84_73,
                           "Transfer To Loan 0001")
    outside = ledger.outside_movements(conn, TERMS)
    assert len(outside) == 2
    assert {r["payee"] for r in outside} == {"Transfer to Apple Pay",
                                            "Transfer To Loan 0001"}


def test_an_excluded_row_is_not_also_called_unpaired(conn, book):
    """It would be counted twice, as a problem and as a decision."""
    ledger.add_transaction(conn, book["checking"], "2026-01-02", -35_00,
                           "Transfer to Apple Pay")
    assert ledger.unpaired_movements(conn, exclude=TERMS) == []
    assert len(ledger.outside_movements(conn, TERMS)) == 1


def test_terms_are_parsed_forgivingly(conn, book):
    assert ledger.split_terms("Apple Pay, venmo ,, LOAN ") == [
        "apple pay", "venmo", "loan"]
    assert ledger.split_terms("") == []
    assert ledger.split_terms(None) == []


def test_no_exclusions_means_nothing_is_excluded(conn, book):
    ledger.add_transaction(conn, book["checking"], "2026-01-02", -35_00,
                           "Transfer to Apple Pay")
    assert len(ledger.unpaired_movements(conn, exclude=[])) == 1
    assert ledger.outside_movements(conn, []) == []
