"""The recommendation engine.

Every expected figure here is worked out by hand. An engine tested only
against its own output proves nothing -- and this is the component that fails
silently rather than loudly, because a wrong budget still looks like a budget.

The fixtures sit entirely in the past. The engine refuses to average over a
month that has not finished, so a window straddling the real current month
would give a different answer depending on the day it was run -- and a
statement dated next December could not exist anyway.
"""

import pytest

from dashboard import ledger, stats
from dashboard import statements as st


# ── pure maths ────────────────────────────────────────────────────────────
def test_trimmed_mean_drops_the_extremes():
    # 12 values: k = 1, so 100 and 1200 go, leaving 200..1100 -> mean 650
    values = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100, 1200]
    assert stats.trimmed_mean(values) == 650


def test_one_outlier_barely_moves_the_trimmed_mean():
    normal = [400] * 11
    assert stats.trimmed_mean([*normal, 4000]) == 400
    assert sum([*normal, 4000]) // 12 == 700, "the plain mean would say 700"


def test_small_samples_still_get_trimmed():
    # Below ten values a strict 10% trims nothing, which is when an outlier
    # does the most damage.
    assert stats.trimmed_mean([10, 20, 30, 40, 1000]) == 30


def test_too_few_values_to_trim_are_left_alone():
    assert stats.trimmed_mean([10, 20, 30, 40]) == 25


def test_trimmed_mean_of_nothing_is_zero():
    assert stats.trimmed_mean([]) == 0


@pytest.mark.parametrize("p,expected", [(0, 10), (25, 20), (50, 30), (100, 50)])
def test_percentiles(p, expected):
    assert stats.percentile([10, 20, 30, 40, 50], p) == expected


def test_mad_is_not_moved_by_a_single_extreme():
    assert stats.mad([10, 12, 11, 13, 500]) == 1


def test_trend_compares_the_halves():
    assert stats.trend_pct([100, 100, 200, 200]) == 100
    assert stats.trend_pct([200, 200, 100, 100]) == -50
    assert stats.trend_pct([100, 100, 100, 100]) == 0


def test_month_arithmetic_crosses_a_year():
    assert stats.prev_month("2025-01") == "2024-12"
    assert stats.month_range("2025-02", 3) == ["2024-12", "2025-01", "2025-02"]


# ── fixtures ──────────────────────────────────────────────────────────────
@pytest.fixture
def book(conn):
    g = ledger.create_category_group(conn, "Everyday")
    b = ledger.create_category_group(conn, "Bills")
    return {
        "acct": ledger.create_account(conn, "Checking"),
        "tracking": ledger.create_account(conn, "Brokerage", on_budget=False),
        "groceries": ledger.create_category(conn, g, "Groceries"),
        "rent": ledger.create_category(conn, b, "Rent"),
        "insurance": ledger.create_category(conn, b, "Insurance"),
        "repairs": ledger.create_category(conn, b, "Repairs"),
        "unused": ledger.create_category(conn, g, "Unused"),
    }


def spend(conn, book, cat, month, pounds, day="10"):
    ledger.add_transaction(conn, book["acct"], "%s-%s" % (month, day),
                           -round(pounds * 100), "Shop", book[cat])


def months(n=12, end="2025-12"):
    return stats.month_range(end, n)


def cover(conn, book, ms):
    """Mark months as having statement data behind them."""
    for m in ms:
        conn.execute(
            "INSERT OR REPLACE INTO month_coverage (account_id, month,"
            " txn_count, first_day, last_day) VALUES (?,?,?,?,?)",
            (book["acct"], m, 1, m + "-01", m + "-28"))
    conn.commit()


# ── classification ────────────────────────────────────────────────────────
def test_a_steady_bill_is_fixed(conn, book):
    ms = months()
    cover(conn, book, ms)
    for m in ms:
        spend(conn, book, "rent", m, 1250)
    a = stats.analyse(conn, book["rent"], end_month="2025-12")
    assert a["kind"] == "fixed"
    assert a["suggested_cents"] == 1250_00
    assert a["confidence"] == "high"


def test_varying_spend_is_variable(conn, book):
    ms = months()
    cover(conn, book, ms)
    for i, m in enumerate(ms):
        spend(conn, book, "groceries", m, 380 + (i % 5) * 25)
    a = stats.analyse(conn, book["groceries"], end_month="2025-12")
    assert a["kind"] == "variable"
    assert a["low_cents"] < a["suggested_cents"] < a["high_cents"]


def test_an_annual_charge_becomes_a_sinking_fund(conn, book):
    """The case a monthly average hides until it arrives."""
    ms = months()
    cover(conn, book, ms)
    spend(conn, book, "insurance", "2025-06", 1200)
    a = stats.analyse(conn, book["insurance"], end_month="2025-12")
    assert a["kind"] == "sinking"
    assert a["suggested_cents"] == 100_00, "1200 over 12 months"
    assert "set aside" in a["basis"]


def test_a_single_charge_saves_rather_than_suggesting_nothing(conn, book):
    """The engine cannot know whether a charge will return.

    Being wrong is lopsided: saving for something that never recurs leaves a
    surplus, budgeting nothing for something annual leaves you short by the
    whole amount. So it saves, says it has seen the charge once, and leaves
    marking it a one-off to the operator.
    """
    ms = months()
    cover(conn, book, ms)
    spend(conn, book, "repairs", "2025-05", 800)
    a = stats.analyse(conn, book["repairs"], end_month="2025-12")
    assert a["kind"] == "sinking"
    assert a["suggested_cents"] == round(800_00 / 12)
    assert a["occurrences"] == 1
    assert "once" in a["basis"]
    assert a["excluded"] == ["2025-05"]


def test_a_category_with_no_spending_declines_to_recommend(conn, book):
    """No evidence, no opinion.

    Recommending zero would read as "budget nothing here", which is wrong for
    a savings goal that has simply not been drawn on yet -- and nothing in
    the data distinguishes a goal from a dead category.
    """
    cover(conn, book, months())
    a = stats.analyse(conn, book["unused"], end_month="2025-12")
    assert a["kind"] == "unused"
    assert a["suggested_cents"] is None


def test_too_little_history_declines_to_guess(conn, book):
    ms = months(2, end="2025-12")
    cover(conn, book, ms)
    for m in ms:
        spend(conn, book, "groceries", m, 400)
    a = stats.analyse(conn, book["groceries"], end_month="2025-12")
    assert a["kind"] == "insufficient"
    # No number at all, rather than a confident one from two months.
    assert a["suggested_cents"] is None
    assert a["confidence"] == "none"


# ── coverage is the load-bearing rule ─────────────────────────────────────
def test_uncovered_months_are_not_averaged_over(conn, book):
    """A month with no statement is not a month with no spending."""
    covered = ["2025-10", "2025-11", "2025-12"]
    cover(conn, book, covered)
    for m in covered:
        spend(conn, book, "groceries", m, 400)
    a = stats.analyse(conn, book["groceries"], end_month="2025-12")
    assert a["sample_months"] == 3
    assert a["months"] == covered
    # Averaging over the nine missing months would give about 100.
    assert a["suggested_cents"] == 400_00


def test_a_gap_lowers_the_stated_confidence(conn, book):
    full = months()
    cover(conn, book, full)
    for m in full:
        spend(conn, book, "groceries", m, 400)
    high = stats.analyse(conn, book["groceries"], end_month="2025-12")

    conn.execute("DELETE FROM month_coverage WHERE month > '2025-06'")
    conn.commit()
    fewer = stats.analyse(conn, book["groceries"], end_month="2025-12")

    assert high["confidence"] == "high"
    assert fewer["sample_months"] < high["sample_months"]
    assert fewer["confidence"] in ("medium", "low")


def test_a_covered_month_with_no_transactions_counts_as_zero(conn, book):
    ms = months(6, end="2025-12")
    cover(conn, book, ms)
    for m in ms[:3]:
        spend(conn, book, "groceries", m, 600)
    a = stats.analyse(conn, book["groceries"], end_month="2025-12")
    # We have statements for all six; three genuinely cost nothing.
    assert a["sample_months"] == 6
    assert a["spend_by_month"][ms[-1]] == 0


# ── what must not be counted ──────────────────────────────────────────────
def test_off_budget_spending_is_ignored(conn, book):
    ms = months()
    cover(conn, book, ms)
    for m in ms:
        spend(conn, book, "groceries", m, 400)
    ledger.add_transaction(conn, book["tracking"], "2025-12-05", -900_00,
                           "Platform fee", book["groceries"])
    a = stats.analyse(conn, book["groceries"], end_month="2025-12")
    assert a["spend_by_month"]["2025-12"] == 400_00


def test_a_refund_reduces_that_months_cost(conn, book):
    ms = months(6, end="2025-12")
    cover(conn, book, ms)
    for m in ms:
        spend(conn, book, "groceries", m, 400)
    ledger.add_transaction(conn, book["acct"], "2025-12-20", 100_00,
                           "Refund", book["groceries"])
    a = stats.analyse(conn, book["groceries"], end_month="2025-12")
    assert a["spend_by_month"]["2025-12"] == 300_00


def test_transfers_are_never_counted_as_spending(conn, book):
    ms = months(6, end="2025-12")
    cover(conn, book, ms)
    other = ledger.create_account(conn, "Savings")
    for m in ms:
        spend(conn, book, "groceries", m, 400)
    ledger.add_transfer(conn, book["acct"], other, "2025-12-15", 500_00)
    a = stats.analyse(conn, book["groceries"], end_month="2025-12")
    assert a["spend_by_month"]["2025-12"] == 400_00


def test_deleted_transactions_do_not_count(conn, book):
    ms = months(6, end="2025-12")
    cover(conn, book, ms)
    for m in ms:
        spend(conn, book, "groceries", m, 400)
    extra = ledger.add_transaction(conn, book["acct"], "2025-12-11", -999_00,
                                   "Mistake", book["groceries"])
    ledger.delete_transaction(conn, extra)
    a = stats.analyse(conn, book["groceries"], end_month="2025-12")
    assert a["spend_by_month"]["2025-12"] == 400_00


# ── reproducibility ───────────────────────────────────────────────────────
def test_the_same_data_gives_the_same_answer(conn, book):
    ms = months()
    cover(conn, book, ms)
    for i, m in enumerate(ms):
        spend(conn, book, "groceries", m, 380 + i * 7)
    a = stats.analyse(conn, book["groceries"], end_month="2025-12")
    b = stats.analyse(conn, book["groceries"], end_month="2025-12")
    # No model, no sampling, no randomness: a budget you cannot reproduce is
    # not a budget.
    assert a == b


def test_analyse_all_covers_every_budgetable_category(conn, book):
    cover(conn, book, months())
    rows = stats.analyse_all(conn, end_month="2025-12")
    names = {r["name"] for r in rows}
    assert {"Groceries", "Rent", "Insurance", "Repairs", "Unused"} <= names
    assert all("group" in r for r in rows)


def test_totals_summarise_the_whole_budget(conn, book):
    ms = months()
    cover(conn, book, ms)
    for m in ms:
        spend(conn, book, "rent", m, 1000)
        spend(conn, book, "groceries", m, 400)
    t = stats.totals(conn, end_month="2025-12")
    assert t["suggested_total_cents"] == 1400_00
    assert t["months"] == 12


# ── it works off imported statements, not just hand entry ─────────────────
def test_recommendations_follow_from_an_import(conn, book):
    rows = ["date,payee,amount"]
    for m in stats.month_range("2025-12", 12):
        rows.append("%s-09,Superstore,-400.00" % m)
    data = ("\n".join(rows) + "\n").encode()
    bid, _ = st.create_batch(conn, book["acct"], "year.csv", data)
    for r in st.batch_rows(conn, bid):
        st.set_row(conn, r["id"], category_id=book["groceries"])
    st.commit_batch(conn, bid)

    a = stats.analyse(conn, book["groceries"], end_month="2025-12")
    assert a["sample_months"] == 12
    assert a["kind"] == "fixed"
    assert a["suggested_cents"] == 400_00


# ── months that cannot honestly be compared ───────────────────────────────
def cover_account(conn, account_id, ms):
    for m in ms:
        conn.execute(
            "INSERT OR REPLACE INTO month_coverage (account_id, month,"
            " txn_count, first_day, last_day) VALUES (?,?,?,?,?)",
            (account_id, m, 1, m + "-01", m + "-28"))
    conn.commit()


def test_the_month_in_progress_is_not_a_data_point(conn, book):
    """Seven days of spending is not a month of it.

    Averaged in as though it were whole, the current month drags every figure
    down -- worst for whatever is billed late in the month, which then looks
    like it got cheaper.
    """
    now = stats.this_month()
    past = stats.month_range(stats.prev_month(now), 3)
    cover(conn, book, [*past, now])
    for m in past:
        spend(conn, book, "groceries", m, 400)
    spend(conn, book, "groceries", now, 30, day="03")

    a = stats.analyse(conn, book["groceries"], end_month=now)
    assert now not in a["months"]
    assert a["sample_months"] == 3
    # Including the part-month would have given about 307.
    assert a["suggested_cents"] == 400_00


def test_a_month_missing_half_your_accounts_is_not_a_cheap_month(conn, book):
    """One statement imported for April and six for July is not frugality."""
    second = ledger.create_account(conn, "Savings")
    full = ["2025-06", "2025-07", "2025-08"]
    thin = ["2025-04", "2025-05"]

    cover_account(conn, book["acct"], thin + full)
    cover_account(conn, second, full)
    for m in full:
        spend(conn, book, "groceries", m, 600)

    a = stats.analyse(conn, book["groceries"], end_month="2025-08")
    assert a["months"] == full
    # Averaging the two thin months in as zeros would have said 360.
    assert a["suggested_cents"] == 600_00


def test_a_new_account_does_not_disqualify_your_history(conn, book):
    """The reference set is the accounts covering half the months, not all.

    Otherwise connecting an account today throws away every month before it,
    and the engine goes quiet exactly when it has the most to say.
    """
    ms = months(6, end="2025-08")
    cover(conn, book, ms)
    for m in ms:
        spend(conn, book, "groceries", m, 500)

    newcomer = ledger.create_account(conn, "Just added")
    cover_account(conn, newcomer, ["2025-08"])

    a = stats.analyse(conn, book["groceries"], end_month="2025-08")
    assert a["sample_months"] == 6
    assert a["suggested_cents"] == 500_00


def test_off_budget_accounts_do_not_affect_comparability(conn, book):
    """A brokerage statement nobody imports must not veto every month."""
    ms = months(4, end="2025-08")
    cover(conn, book, ms)
    for m in ms:
        spend(conn, book, "groceries", m, 300)
    cover_account(conn, book["tracking"], ["2025-08"])

    a = stats.analyse(conn, book["groceries"], end_month="2025-08")
    assert a["sample_months"] == 4


def test_coverage_notes_say_what_was_dropped_and_why(conn, book):
    second = ledger.create_account(conn, "Savings")
    cover_account(conn, book["acct"], ["2025-04", "2025-06", "2025-07", "2025-08"])
    cover_account(conn, second, ["2025-06", "2025-07", "2025-08"])

    notes = stats.coverage_notes(conn, end_month="2025-08")
    assert notes["used"] == ["2025-06", "2025-07", "2025-08"]
    assert notes["enough"] is True
    dropped = {d["month"]: d["reason"] for d in notes["dropped"]}
    assert dropped == {"2025-04": "only 1 of 2 accounts imported"}


def test_coverage_notes_admit_when_there_is_not_enough(conn, book):
    cover(conn, book, ["2025-07", "2025-08"])
    notes = stats.coverage_notes(conn, end_month="2025-08")
    assert notes["enough"] is False
    assert notes["minimum"] == stats.MIN_MONTHS
