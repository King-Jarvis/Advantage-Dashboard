"""Prove the invariant checker actually fires.

A check that has only ever returned "clean" is not known to work. Each test
corrupts the database with raw SQL -- bypassing the ledger API, which would
refuse -- and asserts the corruption is reported.
"""

from dashboard import ledger


def test_clean_ledger_reports_nothing(rawbook):
    c = rawbook["conn"]
    ledger.add_transaction(c, rawbook["checking"], "2026-01-05", -1000,
                           "Shop", rawbook["groceries"])
    ledger.add_transfer(c, rawbook["checking"], rawbook["savings"],
                        "2026-01-06", 5000)
    assert ledger.check_invariants(c) == []


def test_catches_a_transfer_that_no_longer_sums_to_zero(rawbook):
    c = rawbook["conn"]
    out, _ = ledger.add_transfer(c, rawbook["checking"], rawbook["savings"],
                                 "2026-01-06", 5000)
    c.execute("UPDATE transactions SET amount_cents=-9999 WHERE id=?", (out,))
    problems = ledger.check_invariants(c)
    assert any("does not sum to zero" in p for p in problems), problems


def test_catches_a_transfer_that_is_not_mutually_linked(rawbook):
    c = rawbook["conn"]
    out, inn = ledger.add_transfer(c, rawbook["checking"], rawbook["savings"],
                                   "2026-01-06", 5000)
    c.execute("UPDATE transactions SET transfer_id=NULL WHERE id=?", (inn,))
    problems = ledger.check_invariants(c)
    assert any("not mutually linked" in p for p in problems), problems


def test_catches_half_a_deleted_transfer(rawbook):
    c = rawbook["conn"]
    out, inn = ledger.add_transfer(c, rawbook["checking"], rawbook["savings"],
                                   "2026-01-06", 5000)
    c.execute("UPDATE transactions SET deleted=1 WHERE id=?", (inn,))
    problems = ledger.check_invariants(c)
    assert any("one half deleted" in p for p in problems), problems


def test_catches_a_categorised_transfer(rawbook):
    c = rawbook["conn"]
    out, _ = ledger.add_transfer(c, rawbook["checking"], rawbook["savings"],
                                 "2026-01-06", 5000)
    c.execute("UPDATE transactions SET category_id=? WHERE id=?",
              (rawbook["groceries"], out))
    problems = ledger.check_invariants(c)
    assert any("carry a category" in p for p in problems), problems


def test_catches_split_children_that_do_not_sum_to_the_parent(rawbook):
    c = rawbook["conn"]
    p = ledger.add_split(c, rawbook["checking"], "2026-01-12",
                         [(rawbook["groceries"], -6000),
                          (rawbook["fuel"], -4000)])
    child = c.execute("SELECT id FROM transactions WHERE parent_id=? LIMIT 1",
                      (p,)).fetchone()["id"]
    c.execute("UPDATE transactions SET amount_cents=-1 WHERE id=?", (child,))
    problems = ledger.check_invariants(c)
    assert any("children sum" in p for p in problems), problems


def test_reports_every_distinct_problem_at_once(rawbook):
    c = rawbook["conn"]
    out, inn = ledger.add_transfer(c, rawbook["checking"], rawbook["savings"],
                                   "2026-01-06", 5000)
    p = ledger.add_split(c, rawbook["checking"], "2026-01-12",
                         [(rawbook["groceries"], -6000),
                          (rawbook["fuel"], -4000)])
    child = c.execute("SELECT id FROM transactions WHERE parent_id=? LIMIT 1",
                      (p,)).fetchone()["id"]
    c.execute("UPDATE transactions SET amount_cents=-1 WHERE id=?", (child,))
    c.execute("UPDATE transactions SET category_id=? WHERE id=?",
              (rawbook["groceries"], out))
    c.execute("UPDATE transactions SET transfer_id=NULL WHERE id=?", (inn,))
    problems = ledger.check_invariants(c)
    # A checker that stops at the first fault makes repair an iterative slog.
    assert len(problems) >= 3, problems
