"""The ledger: accounts, transactions, and envelope budgeting.

Every function that moves money goes through here, so the invariants live in
one place. They are:

  1. An account's balance is the sum of its rows with parent_id IS NULL.
     Split children carry categorisation, not money. Counting both doubles
     the spending -- the classic split bug.

  2. A transfer is a pair of rows whose amounts sum to zero, each pointing at
     the other, and neither carries a category. Moving your own money is not
     expenditure; categorising it invents spending that never happened.

  3. A split parent's amount equals the sum of its children, exactly. Not
     approximately -- these are integers, so exactness is achievable and
     anything else is a bug.

  4. Deleting one half of a pair, or a split parent, deletes the whole thing.
     Half a transfer is money appearing from nowhere.

check_invariants() asserts all four against the whole database and is run by
the test suite after every scenario.
"""

import time
import uuid

from .money import to_cents
from .text import norm_payee as _norm_payee

__all__ = ["to_cents"]  # re-exported: callers parse money via the ledger


def new_id():
    return uuid.uuid4().hex


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _audit(conn, action, entity_type, entity_id, detail="", actor="system"):
    conn.execute(
        "INSERT INTO audit_log (at, actor, action, entity_type, entity_id, detail)"
        " VALUES (?,?,?,?,?,?)",
        (_now(), actor, action, entity_type, entity_id, detail))


# ═══════════════════════════════════════════════════════════════════════════
#  Accounts and categories
# ═══════════════════════════════════════════════════════════════════════════
def create_account(conn, name, type="checking", on_budget=True):
    aid = new_id()
    conn.execute(
        "INSERT INTO accounts (id, name, type, on_budget, created_at)"
        " VALUES (?,?,?,?,?)",
        (aid, name, type, 1 if on_budget else 0, _now()))
    _audit(conn, "create", "account", aid, name)
    conn.commit()
    return aid


def create_category_group(conn, name, is_income=False, sort=0):
    gid = new_id()
    conn.execute(
        "INSERT INTO category_groups (id, name, is_income, sort) VALUES (?,?,?,?)",
        (gid, name, 1 if is_income else 0, sort))
    conn.commit()
    return gid


def create_category(conn, group_id, name, is_income=False, sort=0,
                    carryover_negative=False):
    cid = new_id()
    conn.execute(
        "INSERT INTO categories (id, group_id, name, is_income, sort,"
        " carryover_negative) VALUES (?,?,?,?,?,?)",
        (cid, group_id, name, 1 if is_income else 0, sort,
         1 if carryover_negative else 0))
    conn.commit()
    return cid


def update_category(conn, category_id, **fields):
    """Rename, re-group, hide, or change carry-over behaviour.

    Hiding rather than deleting is deliberate. A category with history behind
    it cannot be removed without either orphaning those transactions or
    silently rewriting the past; hiding keeps the ledger honest and takes it
    off the screen, which is what was actually wanted.
    """
    allowed = {"name", "group_id", "sort", "hidden", "carryover_negative"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError("cannot update: %s" % ", ".join(sorted(bad)))
    row = conn.execute("SELECT id FROM categories WHERE id=?",
                       (category_id,)).fetchone()
    if row is None:
        raise KeyError(category_id)
    if "name" in fields and not str(fields["name"]).strip():
        raise ValueError("a category needs a name")

    values = [int(v) if k in ("hidden", "carryover_negative", "sort") else v
              for k, v in fields.items()]
    sets = ", ".join("%s=?" % k for k in fields)
    conn.execute("UPDATE categories SET %s WHERE id=?" % sets,
                 [*values, category_id])
    _audit(conn, "update", "category", category_id, ",".join(sorted(fields)))
    conn.commit()


def delete_category(conn, category_id):
    """Remove a category outright, but only while nothing depends on it.

    Anything with history is hidden instead. Deleting it would leave
    transactions pointing at nothing, and a ledger that loses the meaning of
    past spending is worse than a slightly longer list.
    """
    used = conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE category_id=? AND deleted=0",
        (category_id,)).fetchone()["c"]
    budgeted = conn.execute(
        "SELECT COUNT(*) c FROM budget_months WHERE category_id=?"
        "   AND budgeted_cents <> 0", (category_id,)).fetchone()["c"]
    if used or budgeted:
        update_category(conn, category_id, hidden=True)
        return "hidden"
    conn.execute("DELETE FROM budget_months WHERE category_id=?", (category_id,))
    conn.execute("DELETE FROM categories WHERE id=?", (category_id,))
    _audit(conn, "delete", "category", category_id)
    conn.commit()
    return "deleted"


def list_categories(conn, include_hidden=False):
    where = "" if include_hidden else " WHERE c.hidden=0"
    return conn.execute(
        "SELECT c.id, c.name, c.group_id, c.is_income, c.sort, c.hidden,"
        " c.carryover_negative, g.name group_name, g.sort group_sort"
        " FROM categories c JOIN category_groups g ON g.id = c.group_id"
        + where + " ORDER BY g.sort, g.name, c.sort, c.name").fetchall()


# ═══════════════════════════════════════════════════════════════════════════
#  Transactions
# ═══════════════════════════════════════════════════════════════════════════
def add_transaction(conn, account_id, date, amount_cents, payee="",
                    category_id=None, notes="", cleared=False,
                    imported_id=None, source="manual", commit=True):
    """A plain transaction. Negative amount means money out."""
    if not isinstance(amount_cents, int):
        raise TypeError("amount_cents must be int (cents), got %r" % type(amount_cents))
    tid = new_id()
    conn.execute(
        "INSERT INTO transactions (id, account_id, date, amount_cents, payee,"
        " payee_norm, notes, category_id, cleared, imported_id, source,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, account_id, date, amount_cents, payee, _norm_payee(payee), notes,
         category_id, 1 if cleared else 0, imported_id, source, _now(), _now()))
    _audit(conn, "create", "transaction", tid, "%s %d" % (date, amount_cents))
    if commit:
        conn.commit()
    return tid


def add_transfer(conn, from_account_id, to_account_id, date, amount_cents,
                 payee="Transfer", notes="", cleared=False):
    """Move money between two accounts as one linked pair.

    `amount_cents` is the positive magnitude moved. The source row is negative
    and the destination positive, so the pair always sums to zero. Neither row
    carries a category.
    """
    if amount_cents <= 0:
        raise ValueError("transfer amount must be positive; direction comes "
                         "from the account arguments")
    if from_account_id == to_account_id:
        raise ValueError("cannot transfer to the same account")

    out_id, in_id = new_id(), new_id()
    now = _now()
    # The two rows reference each other, so neither can be inserted with its
    # link already set -- the foreign key would point at a row that does not
    # exist yet. Insert both unlinked, then link them. Both statements are in
    # the same transaction, so a pair is never visible half-linked.
    for tid, acct, amt in ((out_id, from_account_id, -amount_cents),
                           (in_id, to_account_id, amount_cents)):
        conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount_cents, payee,"
            " payee_norm, notes, category_id, cleared, transfer_id, source,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,NULL,?,NULL,?,?,?)",
            (tid, acct, date, amt, payee, _norm_payee(payee), notes,
             1 if cleared else 0, "transfer", now, now))
    conn.execute("UPDATE transactions SET transfer_id=? WHERE id=?", (in_id, out_id))
    conn.execute("UPDATE transactions SET transfer_id=? WHERE id=?", (out_id, in_id))
    _audit(conn, "create", "transfer", out_id, "%s %d" % (date, amount_cents))
    conn.commit()
    return out_id, in_id


def add_split(conn, account_id, date, splits, payee="", notes="",
              cleared=False, imported_id=None, source="manual"):
    """One transaction divided across categories.

    `splits` is [(category_id, amount_cents), ...]. The parent's amount is the
    sum, and the parent carries no category. Balance counts the parent only.
    """
    if not splits:
        raise ValueError("a split needs at least one part")
    total = sum(a for _, a in splits)
    if not all(isinstance(a, int) for _, a in splits):
        raise TypeError("split amounts must be int cents")

    now = _now()
    parent = new_id()
    conn.execute(
        "INSERT INTO transactions (id, account_id, date, amount_cents, payee,"
        " payee_norm, notes, category_id, cleared, imported_id, source,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,NULL,?,?,?,?,?)",
        (parent, account_id, date, total, payee, _norm_payee(payee), notes,
         1 if cleared else 0, imported_id, source, now, now))
    for cid, amt in splits:
        conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount_cents, payee,"
            " payee_norm, notes, category_id, cleared, parent_id, source,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id(), account_id, date, amt, payee, _norm_payee(payee), "",
             cid, 1 if cleared else 0, parent, source, now, now))
    _audit(conn, "create", "split", parent, "%s %d in %d parts"
           % (date, total, len(splits)))
    conn.commit()
    return parent


def delete_transaction(conn, txn_id):
    """Soft-delete, taking the whole unit with it.

    Deleting half a transfer would make money appear from nowhere; deleting a
    split parent while leaving children would leave categorised amounts with
    no money behind them.
    """
    row = conn.execute(
        "SELECT id, transfer_id, parent_id FROM transactions WHERE id=? AND deleted=0",
        (txn_id,)).fetchone()
    if row is None:
        return 0

    ids = {row["id"]}
    if row["transfer_id"]:
        ids.add(row["transfer_id"])
    # Deleting a child alone is not meaningful: remove the whole split.
    root = row["parent_id"] or row["id"]
    ids.add(root)
    ids.update(r["id"] for r in conn.execute(
        "SELECT id FROM transactions WHERE parent_id=? AND deleted=0", (root,)))

    now = _now()
    conn.executemany("UPDATE transactions SET deleted=1, updated_at=? WHERE id=?",
                     [(now, i) for i in ids])
    _audit(conn, "delete", "transaction", txn_id, "removed %d row(s)" % len(ids))
    conn.commit()
    return len(ids)


def update_transaction(conn, txn_id, **fields):
    """Edit an existing transaction.

    Refuses fields that would break a pairing: amount and account on a
    transfer must change through both halves, and a transfer can never gain a
    category.
    """
    allowed = {"date", "amount_cents", "payee", "notes", "category_id",
               "cleared", "reconciled"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError("cannot update: %s" % ", ".join(sorted(bad)))

    row = conn.execute("SELECT * FROM transactions WHERE id=? AND deleted=0",
                       (txn_id,)).fetchone()
    if row is None:
        raise KeyError(txn_id)
    if row["transfer_id"] and "category_id" in fields and fields["category_id"]:
        raise ValueError("a transfer cannot be categorised")
    if row["reconciled"] and ("amount_cents" in fields or "date" in fields):
        raise ValueError("reconciled transactions cannot change amount or date")

    has_children = conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE parent_id=? AND deleted=0",
        (txn_id,)).fetchone()["c"] > 0

    if "amount_cents" in fields:
        if not isinstance(fields["amount_cents"], int):
            raise TypeError("amount_cents must be int cents")
        if row["transfer_id"]:
            conn.execute("UPDATE transactions SET amount_cents=?, updated_at=?"
                         " WHERE id=?",
                         (-fields["amount_cents"], _now(), row["transfer_id"]))
        if has_children:
            raise ValueError("change the split parts, not the parent total")

    if "date" in fields:
        # A split is one event. Moving the parent without its children would
        # put the money in one month and the categorised spending in another,
        # so the envelope shows spending in a month the money never left.
        if row["parent_id"]:
            raise ValueError("a split part takes its date from the parent; "
                             "move the parent instead")
        if has_children:
            conn.execute("UPDATE transactions SET date=?, updated_at=?"
                         " WHERE parent_id=? AND deleted=0",
                         (fields["date"], _now(), txn_id))
        if row["transfer_id"]:
            # Both halves of a transfer are one movement on one day.
            conn.execute("UPDATE transactions SET date=?, updated_at=?"
                         " WHERE id=?",
                         (fields["date"], _now(), row["transfer_id"]))

    sets = ", ".join("%s=?" % k for k in fields)
    vals = [int(v) if k in ("cleared", "reconciled") else v
            for k, v in fields.items()]
    if "payee" in fields:
        sets += ", payee_norm=?"
        vals.append(_norm_payee(fields["payee"]))
    conn.execute("UPDATE transactions SET %s, updated_at=? WHERE id=?" % sets,
                 [*vals, _now(), txn_id])
    _audit(conn, "update", "transaction", txn_id, ",".join(sorted(fields)))
    conn.commit()


# ═══════════════════════════════════════════════════════════════════════════
#  Balances
# ═══════════════════════════════════════════════════════════════════════════
def account_balance(conn, account_id, as_of=None):
    """Sum of top-level rows. Split children are excluded by parent_id."""
    q = ("SELECT COALESCE(SUM(amount_cents),0) b FROM transactions"
         " WHERE account_id=? AND deleted=0 AND parent_id IS NULL")
    args = [account_id]
    if as_of:
        q += " AND date <= ?"
        args.append(as_of)
    return conn.execute(q, args).fetchone()["b"]


def category_activity(conn, category_id, month):
    """Signed movement for a category in a month. Negative means spent.

    Off-budget accounts are excluded. A tracking account -- an investment or a
    loan -- is recorded so net worth is right, not so its movements consume
    this month's grocery money. Without this join, brokerage fees categorised
    for reporting would silently drain a real envelope.
    """
    return conn.execute(
        "SELECT COALESCE(SUM(t.amount_cents),0) a FROM transactions t"
        " JOIN accounts a ON a.id = t.account_id"
        " WHERE t.category_id=? AND t.deleted=0 AND a.on_budget=1"
        "   AND substr(t.date,1,7)=?",
        (category_id, month)).fetchone()["a"]


def set_budget(conn, month, category_id, cents):
    if not isinstance(cents, int):
        raise TypeError("budget must be int cents")
    conn.execute(
        "INSERT INTO budget_months (month, category_id, budgeted_cents)"
        " VALUES (?,?,?) ON CONFLICT(month, category_id)"
        " DO UPDATE SET budgeted_cents=excluded.budgeted_cents",
        (month, category_id, cents))
    _audit(conn, "budget", "category", category_id, "%s = %d" % (month, cents))
    conn.commit()


def get_budget(conn, month, category_id):
    r = conn.execute("SELECT budgeted_cents b FROM budget_months"
                     " WHERE month=? AND category_id=?",
                     (month, category_id)).fetchone()
    return r["b"] if r else 0


def _months_through(conn, month):
    """Every month with activity or a budget, up to and including `month`."""
    rows = conn.execute(
        "SELECT DISTINCT substr(date,1,7) m FROM transactions WHERE deleted=0"
        " UNION SELECT DISTINCT month FROM budget_months").fetchall()
    return sorted({r["m"] for r in rows if r["m"] and r["m"] <= month} | {month})


def category_balance(conn, category_id, month):
    """Envelope balance: carry-in + budgeted + activity, walked from the start.

    A month that ends negative either carries the debt forward or is absorbed
    by To Be Budgeted, depending on the category's carryover_negative flag.
    Absorbing is the default because most people expect an overspend to be
    settled, not to haunt the envelope.
    """
    cat = conn.execute("SELECT carryover_negative FROM categories WHERE id=?",
                       (category_id,)).fetchone()
    if cat is None:
        raise KeyError(category_id)
    carry = 0
    for m in _months_through(conn, month):
        bal = carry + get_budget(conn, m, category_id) + \
            category_activity(conn, category_id, m)
        if m == month:
            return bal
        carry = bal if (bal >= 0 or cat["carryover_negative"]) else 0
    # _months_through always includes `month`, so the loop always returns.
    # Stating that as a failure rather than returning a plausible-looking
    # number means a future change to _months_through cannot quietly make
    # this function wrong.
    raise AssertionError("unreachable: _months_through omitted %s" % month)


def absorbed_overspend_through(conn, month):
    """Total overspend that To Be Budgeted has had to swallow, up to `month`."""
    total = 0
    cats = conn.execute("SELECT id, carryover_negative FROM categories"
                        " WHERE is_income=0").fetchall()
    for c in cats:
        if c["carryover_negative"]:
            continue
        carry = 0
        for m in _months_through(conn, month):
            bal = carry + get_budget(conn, m, c["id"]) + \
                category_activity(conn, c["id"], m)
            if bal < 0:
                total += -bal
                carry = 0
            else:
                carry = bal
    return total


def payee_groups(conn, filed=False, limit=300):
    """What is still unfiled, grouped by merchant, biggest first.

    Grouped because that is how the work actually divides: twelve payroll
    deposits are one decision, and a list of a hundred and twenty-three rows
    invites you to make it a hundred and twenty-three times.
    """
    from .text import norm_payee
    where = "t.category_id IS NULL" if not filed else "t.category_id IS NOT NULL"
    rows = conn.execute(
        "SELECT t.id, t.payee, t.amount_cents, t.date, t.category_id,"
        "       c.name category_name FROM transactions t"
        " JOIN accounts a ON a.id = t.account_id"
        " LEFT JOIN categories c ON c.id = t.category_id"
        " WHERE t.deleted=0 AND %s"
        "   AND t.transfer_id IS NULL AND t.parent_id IS NULL"
        " ORDER BY t.date DESC" % where).fetchall()
    groups = {}
    for r in rows:
        key = norm_payee(r["payee"] or "")
        g = groups.setdefault(key, {
            "key": key, "example": r["payee"] or "", "count": 0,
            "total_cents": 0, "latest": r["date"],
            "category_id": r["category_id"], "category": r["category_name"],
            "mixed": False})
        g["count"] += 1
        g["total_cents"] += r["amount_cents"]
        # A merchant filed two different ways is worth flagging rather than
        # showing one of the two as though it were the whole story.
        if r["category_id"] != g["category_id"]:
            g["mixed"] = True
    return sorted(groups.values(), key=lambda g: -abs(g["total_cents"]))[:limit]


def categorise_payee(conn, payee_key, category_id, overwrite=False):
    """File every row for one merchant at once.

    By default only rows without a category are touched: clearing a backlog
    must not silently rewrite decisions already made. `overwrite` is for the
    deliberate case -- you looked at a merchant you had already filed and
    changed your mind about it.
    """
    from .text import norm_payee
    if conn.execute("SELECT 1 FROM categories WHERE id=?",
                    (category_id,)).fetchone() is None:
        raise KeyError(category_id)
    clause = "" if overwrite else " AND category_id IS NULL"
    rows = conn.execute(
        "SELECT id, payee FROM transactions"
        " WHERE deleted=0" + clause +
        "   AND transfer_id IS NULL AND parent_id IS NULL").fetchall()
    n = 0
    for r in rows:
        if norm_payee(r["payee"] or "") != payee_key:
            continue
        conn.execute("UPDATE transactions SET category_id=? WHERE id=?",
                     (category_id, r["id"]))
        n += 1
    if n:
        _audit(conn, "update", "transaction", payee_key,
               "categorised %d rows" % n)
    conn.commit()
    return n


def to_be_budgeted(conn, month):
    """Money that has arrived but has not yet been given a job."""
    end = month + "-31"
    income = conn.execute(
        "SELECT COALESCE(SUM(t.amount_cents),0) s FROM transactions t"
        " JOIN categories c ON c.id = t.category_id"
        " JOIN accounts a ON a.id = t.account_id"
        " WHERE t.deleted=0 AND c.is_income=1 AND a.on_budget=1 AND t.date <= ?",
        (end,)).fetchone()["s"]
    budgeted = conn.execute(
        "SELECT COALESCE(SUM(budgeted_cents),0) s FROM budget_months"
        " WHERE month <= ?", (month,)).fetchone()["s"]
    return income - budgeted - absorbed_overspend_through(conn, month)


def move_money(conn, month, from_category_id, to_category_id, cents):
    """Move budgeted money between envelopes. The month's total is unchanged."""
    if cents <= 0:
        raise ValueError("move amount must be positive")
    if from_category_id == to_category_id:
        raise ValueError("cannot move money to the same category")
    set_budget(conn, month, from_category_id,
               get_budget(conn, month, from_category_id) - cents)
    set_budget(conn, month, to_category_id,
               get_budget(conn, month, to_category_id) + cents)


# ═══════════════════════════════════════════════════════════════════════════
#  Invariants
# ═══════════════════════════════════════════════════════════════════════════
def check_invariants(conn):
    """Return a list of violations. Empty means the ledger is internally sound."""
    bad = []

    # A split child must sit in the same account and on the same date as its
    # parent. Drift here would make the parent's money and the children's
    # categorisation describe different events.
    for r in conn.execute(
            "SELECT c.id, c.account_id c_acct, c.date c_date,"
            "       p.account_id p_acct, p.date p_date"
            " FROM transactions c JOIN transactions p ON p.id = c.parent_id"
            " WHERE c.deleted=0 AND p.deleted=0"):
        if r["c_acct"] != r["p_acct"]:
            bad.append("split child %s is in a different account from its parent"
                       % r["id"][:8])
        if r["c_date"] != r["p_date"]:
            bad.append("split child %s has a different date from its parent"
                       % r["id"][:8])

    # An orphan child -- parent deleted, child still live -- is categorised
    # spending with no money behind it.
    n = conn.execute("SELECT COUNT(*) c FROM transactions t"
                     " JOIN transactions p ON p.id = t.parent_id"
                     " WHERE t.deleted=0 AND p.deleted=1").fetchone()["c"]
    if n:
        bad.append("%d split child/children outlived their parent" % n)

    for r in conn.execute(
            "SELECT a.id a_id, a.amount_cents a_amt, b.id b_id, b.amount_cents b_amt,"
            " b.transfer_id b_back FROM transactions a"
            " JOIN transactions b ON b.id = a.transfer_id"
            " WHERE a.deleted=0 AND a.transfer_id IS NOT NULL"):
        if r["a_amt"] + r["b_amt"] != 0:
            bad.append("transfer %s/%s does not sum to zero: %d + %d"
                       % (r["a_id"][:8], r["b_id"][:8], r["a_amt"], r["b_amt"]))
        if r["b_back"] != r["a_id"]:
            bad.append("transfer %s is not mutually linked" % r["a_id"][:8])

    for r in conn.execute(
            "SELECT id, amount_cents FROM transactions"
            " WHERE deleted=0 AND parent_id IS NULL"
            "   AND id IN (SELECT parent_id FROM transactions WHERE deleted=0"
            "              AND parent_id IS NOT NULL)"):
        kids = conn.execute("SELECT COALESCE(SUM(amount_cents),0) s FROM transactions"
                            " WHERE parent_id=? AND deleted=0",
                            (r["id"],)).fetchone()["s"]
        if kids != r["amount_cents"]:
            bad.append("split %s: children sum %d != parent %d"
                       % (r["id"][:8], kids, r["amount_cents"]))

    n = conn.execute("SELECT COUNT(*) c FROM transactions"
                     " WHERE deleted=0 AND transfer_id IS NOT NULL"
                     "   AND category_id IS NOT NULL").fetchone()["c"]
    if n:
        bad.append("%d transfer row(s) carry a category" % n)

    n = conn.execute("SELECT COUNT(*) c FROM transactions t"
                     " JOIN transactions p ON p.id = t.transfer_id"
                     " WHERE t.deleted=0 AND p.deleted=1").fetchone()["c"]
    if n:
        bad.append("%d transfer(s) have one half deleted" % n)

    return bad
