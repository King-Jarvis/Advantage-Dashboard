"""A randomised walk over the ledger.

Hand-written tests check the cases someone thought of. This runs a few hundred
arbitrary operations and asserts the invariants after every single one, which
is how the interaction between operations gets exercised -- deleting one half
of a transfer that was itself edited after a split was recategorised is not a
scenario anyone writes by hand.

Seeded, so a failure is reproducible rather than a story about a flaky test.
"""

import random

import pytest

from dashboard import ledger

OPS = 400
SEED = 20260831


@pytest.mark.parametrize("seed", [SEED, SEED + 7, SEED + 13, SEED + 29])
def test_random_operations_never_break_the_invariants(rawconn, seed):
    rng = random.Random(seed)
    c = rawconn

    accounts = [ledger.create_account(c, "Acct%d" % i,
                                      on_budget=(i != 3)) for i in range(4)]
    g = ledger.create_category_group(c, "Group")
    gi = ledger.create_category_group(c, "Inc", is_income=True)
    cats = [ledger.create_category(c, g, "Cat%d" % i,
                                   carryover_negative=(i % 3 == 0))
            for i in range(5)]
    income = ledger.create_category(c, gi, "Salary", is_income=True)
    months = ["2026-%02d" % m for m in range(1, 7)]
    live = []

    def a_date():
        return "%s-%02d" % (rng.choice(months), rng.randint(1, 28))

    for step in range(OPS):
        op = rng.choices(
            ["txn", "transfer", "split", "delete", "update", "budget", "move"],
            weights=[30, 12, 12, 14, 14, 12, 6])[0]
        try:
            if op == "txn":
                live.append(ledger.add_transaction(
                    c, rng.choice(accounts), a_date(),
                    rng.randint(-50_000, 50_000),
                    payee="P%d" % rng.randint(0, 20),
                    category_id=rng.choice(cats + [income, None])))
            elif op == "transfer":
                a, b = rng.sample(accounts, 2)
                out, _ = ledger.add_transfer(c, a, b, a_date(),
                                             rng.randint(1, 30_000))
                live.append(out)
            elif op == "split":
                parts = [(rng.choice(cats), rng.randint(-20_000, -1))
                         for _ in range(rng.randint(2, 4))]
                live.append(ledger.add_split(c, rng.choice(accounts),
                                             a_date(), parts))
            elif op == "delete" and live:
                ledger.delete_transaction(c, live.pop(rng.randrange(len(live))))
            elif op == "update" and live:
                t = rng.choice(live)
                field = rng.choice(["date", "payee", "notes", "cleared",
                                    "category_id", "amount_cents"])
                value = {"date": a_date(), "payee": "Q%d" % rng.randint(0, 9),
                         "notes": "n", "cleared": rng.choice([True, False]),
                         "category_id": rng.choice(cats),
                         "amount_cents": rng.randint(-40_000, 40_000)}[field]
                ledger.update_transaction(c, t, **{field: value})
            elif op == "budget":
                ledger.set_budget(c, rng.choice(months), rng.choice(cats),
                                  rng.randint(0, 200_000))
            elif op == "move":
                x, y = rng.sample(cats, 2)
                ledger.move_money(c, rng.choice(months), x, y,
                                  rng.randint(1, 20_000))
        except (ValueError, KeyError):
            # Refusals are the point -- a categorised transfer, an edit to a
            # split parent's total. The ledger saying no is correct behaviour,
            # not a failure of the walk.
            pass

        problems = ledger.check_invariants(c)
        assert problems == [], "seed %d step %d (%s) broke: %s" % (seed, step, op, problems)


def test_transfers_never_change_total_net_worth(rawconn):
    rng = random.Random(SEED + 1)
    c = rawconn
    accounts = [ledger.create_account(c, "A%d" % i) for i in range(3)]
    g = ledger.create_category_group(c, "G")
    cat = ledger.create_category(c, g, "C")

    for _ in range(40):
        ledger.add_transaction(c, rng.choice(accounts),
                               "2026-01-%02d" % rng.randint(1, 28),
                               rng.randint(-20_000, 20_000), category_id=cat)
    before = sum(ledger.account_balance(c, a) for a in accounts)

    for _ in range(40):
        a, b = rng.sample(accounts, 2)
        ledger.add_transfer(c, a, b, "2026-02-01", rng.randint(1, 10_000))

    after = sum(ledger.account_balance(c, a) for a in accounts)
    assert after == before, "moving money between your own accounts created or destroyed some"
    assert ledger.check_invariants(c) == []


def test_deleting_everything_returns_to_zero(rawconn):
    rng = random.Random(SEED + 2)
    c = rawconn
    acct = ledger.create_account(c, "A")
    other = ledger.create_account(c, "B")
    g = ledger.create_category_group(c, "G")
    cat = ledger.create_category(c, g, "C")

    ids = []
    for _ in range(30):
        kind = rng.choice(["txn", "transfer", "split"])
        if kind == "txn":
            ids.append(ledger.add_transaction(c, acct, "2026-01-05",
                                              rng.randint(-9999, 9999),
                                              category_id=cat))
        elif kind == "transfer":
            ids.append(ledger.add_transfer(c, acct, other, "2026-01-05",
                                           rng.randint(1, 9999))[0])
        else:
            ids.append(ledger.add_split(c, acct, "2026-01-05",
                                        [(cat, -500), (cat, -700)]))
    for i in ids:
        ledger.delete_transaction(c, i)

    assert ledger.account_balance(c, acct) == 0
    assert ledger.account_balance(c, other) == 0
    assert ledger.category_activity(c, cat, "2026-01") == 0
    assert ledger.check_invariants(c) == []
