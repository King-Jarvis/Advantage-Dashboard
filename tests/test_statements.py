"""Reading statements: the formats banks actually produce, and the traps."""

import pytest

from dashboard import ledger
from dashboard import statements as st

UK = b"""Transaction Date,Description,Paid Out,Paid In,Balance
04/08/2026,TESCO STORES 3299,42.15,,1000.00
05/08/2026,SALARY ACME LTD,,2500.00,3500.00
06/08/2026,SHELL FILLING STN,61.20,,3438.80
"""

US = b"""Date,Description,Amount
08/04/2026,GREEN GROCER #123,-42.15
08/05/2026,PAYROLL ACME,2500.00
"""

ISO = b"""date,payee,amount,notes
2026-08-04,Corner Market,-12.34,groceries
2026-08-05,Refund,5.00,
"""

OFX = b"""OFXHEADER:100
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260804120000<TRNAMT>-42.15
<FITID>ABC-1<NAME>TESCO STORES<MEMO>card purchase</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260805<TRNAMT>2500.00
<FITID>ABC-2<NAME>SALARY</STMTTRN>
</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""


@pytest.fixture
def book(conn):
    g = ledger.create_category_group(conn, "Everyday")
    return {"acct": ledger.create_account(conn, "Checking"),
            "other": ledger.create_account(conn, "Savings"),
            "groceries": ledger.create_category(conn, g, "Groceries")}


# ── format detection ──────────────────────────────────────────────────────
def test_detects_ofx_from_content_not_filename():
    kind, _ = st.sniff(OFX)
    assert kind == "ofx"
    kind, _ = st.sniff(UK)
    assert kind == "csv"


def test_rejects_an_oversized_file():
    with pytest.raises(st.ParseError):
        st.sniff(b"x" * (st.MAX_BYTES + 1))


def test_rejects_an_empty_file():
    with pytest.raises(st.ParseError):
        st.sniff(b"   ")


def test_handles_a_utf8_bom():
    rows, _ = st.parse(b"\xef\xbb\xbf" + ISO)
    assert rows[0]["payee"] == "Corner Market"


# ── separate debit and credit columns ─────────────────────────────────────
def test_debit_and_credit_columns_become_signed_amounts(book):
    rows, meta = st.parse(UK)
    assert [r["amount_cents"] for r in rows] == [-4215, 250000, -6120]
    assert meta["date_format"] == "%d/%m/%Y"


def test_single_amount_column(book):
    rows, _ = st.parse(US)
    assert [r["amount_cents"] for r in rows] == [-4215, 250000]


def test_a_row_with_both_debit_and_credit_is_flagged_not_guessed():
    bad = b"Date,Description,Debit,Credit\n01/08/2026,Odd,10.00,5.00\n"
    rows, _ = st.parse(bad)
    assert rows[0]["error"], "an ambiguous row must be reported, not resolved"


# ── date formats ──────────────────────────────────────────────────────────
def test_unambiguous_day_first_is_detected():
    fmt, survivors = st.detect_date_format(["25/12/2026", "04/08/2026"])
    assert fmt == "%d/%m/%Y"
    assert "%m/%d/%Y" not in survivors, "a 25th month cannot exist"


def test_unambiguous_month_first_is_detected():
    fmt, _ = st.detect_date_format(["12/25/2026", "08/04/2026"])
    assert fmt == "%m/%d/%Y"


def test_genuinely_ambiguous_dates_report_both_candidates():
    """03/04 is a real ambiguity, and the caller must be told so."""
    _, survivors = st.detect_date_format(["03/04/2026", "05/06/2026"])
    assert "%d/%m/%Y" in survivors and "%m/%d/%Y" in survivors


def test_unparseable_dates_raise_rather_than_guess():
    with pytest.raises(st.ParseError):
        st.detect_date_format(["not a date", "also not"])


def test_a_bad_row_is_reported_and_the_rest_still_parse():
    mixed = b"date,payee,amount\n2026-08-04,Good,-10.00\nrubbish,Bad,-5.00\n"
    rows, _ = st.parse(mixed)
    assert rows[0]["error"] == "" and rows[1]["error"]
    # 47 of 48 rows importing silently is worse than refusing outright.
    assert rows[1]["date"] is None


# ── payee normalisation ───────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("TESCO STORES 3299", "tesco stores"),
    ("tesco  stores", "tesco stores"),
    ("CARD PURCHASE SHELL 00123456", "shell"),
    ("Amazon.co.uk*A12BC", "amazon co uk a12bc"),
])
def test_payee_normalisation_strips_bank_decoration(raw, expected):
    assert st.norm_payee(raw) == expected


# ── missing columns ───────────────────────────────────────────────────────
def test_a_file_with_no_date_column_is_refused():
    with pytest.raises(st.ParseError):
        st.parse(b"description,amount\nShop,-10.00\n")


def test_a_file_with_no_amount_column_is_refused():
    with pytest.raises(st.ParseError):
        st.parse(b"date,description\n2026-08-04,Shop\n")


# ── batches ───────────────────────────────────────────────────────────────
def test_a_batch_writes_nothing_to_the_ledger(conn, book):
    st.create_batch(conn, book["acct"], "aug.csv", UK)
    assert ledger.account_balance(conn, book["acct"]) == 0, \
        "review must happen before anything is written"


def test_committing_writes_the_included_rows(conn, book):
    bid, _ = st.create_batch(conn, book["acct"], "aug.csv", UK)
    assert st.commit_batch(conn, bid) == 3
    assert ledger.account_balance(conn, book["acct"]) == -4215 + 250000 - 6120


def test_committing_twice_imports_nothing_the_second_time(conn, book):
    bid, _ = st.create_batch(conn, book["acct"], "aug.csv", UK)
    st.commit_batch(conn, bid)
    before = ledger.account_balance(conn, book["acct"])
    assert st.commit_batch(conn, bid) == 0
    assert ledger.account_balance(conn, book["acct"]) == before


def test_reimporting_the_same_file_finds_every_row_a_duplicate(conn, book):
    bid, _ = st.create_batch(conn, book["acct"], "aug.csv", UK)
    st.commit_batch(conn, bid)
    bid2, _ = st.create_batch(conn, book["acct"], "aug-again.csv", UK)
    rows = st.batch_rows(conn, bid2)
    assert all(r["is_duplicate"] for r in rows)
    assert all(r["dup_kind"] == "exact" for r in rows)
    # Excluded by default: the safe default is not to import.
    assert all(r["excluded"] for r in rows)
    assert st.commit_batch(conn, bid2) == 0


def test_two_identical_purchases_on_one_day_are_not_a_duplicate(conn, book):
    """Two coffees at the same shop for the same price is two transactions."""
    twice = (b"date,payee,amount\n2026-08-04,Cafe,-3.50\n"
             b"2026-08-04,Cafe,-3.50\n")
    bid, _ = st.create_batch(conn, book["acct"], "c.csv", twice)
    rows = st.batch_rows(conn, bid)
    assert not any(r["is_duplicate"] for r in rows)
    assert st.commit_batch(conn, bid) == 2
    assert ledger.account_balance(conn, book["acct"]) == -700


def test_a_redated_transaction_is_caught_as_a_near_duplicate(conn, book):
    ledger.add_transaction(conn, book["acct"], "2026-08-04", -4215,
                           "TESCO STORES 3299")
    bid, _ = st.create_batch(conn, book["acct"], "aug.csv", UK)
    row = [r for r in st.batch_rows(conn, bid) if r["line_no"] == 2][0]
    assert row["is_duplicate"] and row["dup_kind"] == "near"


def test_the_same_row_in_a_different_account_is_not_a_duplicate(conn, book):
    bid, _ = st.create_batch(conn, book["acct"], "a.csv", UK)
    st.commit_batch(conn, bid)
    bid2, _ = st.create_batch(conn, book["other"], "a.csv", UK)
    assert not any(r["is_duplicate"] for r in st.batch_rows(conn, bid2))


def test_excluding_a_row_keeps_it_out(conn, book):
    bid, _ = st.create_batch(conn, book["acct"], "aug.csv", UK)
    rows = st.batch_rows(conn, bid)
    st.set_row(conn, rows[0]["id"], excluded=True)
    assert st.commit_batch(conn, bid) == 2


def test_a_category_chosen_at_review_is_applied(conn, book):
    bid, _ = st.create_batch(conn, book["acct"], "aug.csv", UK)
    rows = st.batch_rows(conn, bid)
    st.set_row(conn, rows[0]["id"], category_id=book["groceries"])
    st.commit_batch(conn, bid)
    assert ledger.category_activity(conn, book["groceries"], "2026-08") == -4215


def test_set_row_refuses_unknown_fields(conn, book):
    bid, _ = st.create_batch(conn, book["acct"], "aug.csv", UK)
    row = st.batch_rows(conn, bid)[0]
    with pytest.raises(ValueError):
        st.set_row(conn, row["id"], txn_id="sneaky")


def test_discarding_a_batch_leaves_the_ledger_alone(conn, book):
    bid, _ = st.create_batch(conn, book["acct"], "aug.csv", UK)
    st.discard_batch(conn, bid)
    assert st.batch_rows(conn, bid) == []
    assert ledger.account_balance(conn, book["acct"]) == 0


# ── OFX ───────────────────────────────────────────────────────────────────
def test_ofx_parses_and_uses_the_banks_own_id(conn, book):
    rows, meta = st.parse(OFX)
    assert meta["kind"] == "ofx"
    assert [r["amount_cents"] for r in rows] == [-4215, 250000]
    assert rows[0]["fitid"] == "ABC-1"
    # The bank's id is a better identity than anything we could derive.
    a = st.dedup_key(book["acct"], rows[0])
    b = st.dedup_key(book["acct"], dict(rows[0], date="2026-09-09"))
    assert a == b, "FITID identity must not depend on the date we parsed"


def test_ofx_round_trip_through_a_batch(conn, book):
    bid, _ = st.create_batch(conn, book["acct"], "s.ofx", OFX)
    assert st.commit_batch(conn, bid) == 2
    assert ledger.account_balance(conn, book["acct"]) == -4215 + 250000


# ── remembered mappings ───────────────────────────────────────────────────
def test_a_mapping_is_remembered_per_bank(conn, book):
    _, meta = st.parse(UK)
    st.remember_mapping(conn, meta, label="Test Bank")
    got = st.recall_mapping(conn, meta["fingerprint"])
    assert got["label"] == "Test Bank"
    assert got["date_format"] == "%d/%m/%Y"


def test_different_banks_get_different_fingerprints():
    _, uk = st.parse(UK)
    _, us = st.parse(US)
    assert uk["fingerprint"] != us["fingerprint"]


def test_column_order_does_not_change_the_fingerprint():
    a = st.fingerprint(["Date", "Amount", "Description"])
    b = st.fingerprint(["Description", "Date", "Amount"])
    assert a == b


# ── coverage ──────────────────────────────────────────────────────────────
def test_coverage_reflects_what_was_imported(conn, book):
    bid, _ = st.create_batch(conn, book["acct"], "aug.csv", UK)
    st.commit_batch(conn, bid)
    cov = st.coverage(conn, book["acct"])
    assert [c["month"] for c in cov] == ["2026-08"]
    assert cov[0]["txn_count"] == 3


def test_gaps_are_reported_between_covered_months(conn, book):
    for month in ("06", "09"):
        data = ("date,payee,amount\n2026-%s-10,Shop,-10.00\n" % month).encode()
        bid, _ = st.create_batch(conn, book["acct"], "x.csv", data)
        st.commit_batch(conn, bid)
    # July and August have no statement behind them, and the engine must not
    # average over a month it does not have.
    assert st.gaps(conn, book["acct"]) == ["2026-07", "2026-08"]


def test_no_gaps_when_months_are_contiguous(conn, book):
    for month in ("06", "07"):
        data = ("date,payee,amount\n2026-%s-10,Shop,-10.00\n" % month).encode()
        bid, _ = st.create_batch(conn, book["acct"], "x.csv", data)
        st.commit_batch(conn, bid)
    assert st.gaps(conn, book["acct"]) == []


# ── the ledger and the importer must agree ────────────────────────────────
@pytest.mark.parametrize("payee", [
    "TESCO STORES 3299", "Card Purchase SHELL 00123456",
    "Amazon.co.uk*A12BC", "  Corner   Market  ", "SALARY ACME LTD",
])
def test_stored_and_computed_payee_norm_are_identical(conn, book, payee):
    """The bug this guards against was silent.

    The ledger normalised payees one way and the importer another, so a
    re-imported statement looked like new spending and the only symptom was a
    budget that drifted. Both now use dashboard.text.norm_payee; this asserts
    they still do.
    """
    txn = ledger.add_transaction(conn, book["acct"], "2026-08-04", -1000, payee)
    stored = conn.execute("SELECT payee_norm FROM transactions WHERE id=?",
                          (txn,)).fetchone()["payee_norm"]
    assert stored == st.norm_payee(payee)


def test_near_duplicate_detection_survives_bank_decoration(conn, book):
    # The same purchase, described differently by two exports.
    ledger.add_transaction(conn, book["acct"], "2026-08-04", -4215,
                           "CARD PURCHASE TESCO STORES 998877")
    data = b"date,payee,amount\n2026-08-05,TESCO STORES 3299,-42.15\n"
    bid, _ = st.create_batch(conn, book["acct"], "x.csv", data)
    row = st.batch_rows(conn, bid)[0]
    assert row["is_duplicate"] and row["dup_kind"] == "near"
