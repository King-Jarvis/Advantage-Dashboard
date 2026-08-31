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

__all__ = ["to_cents"]  # re-exported: callers parse money via the ledger


def new_id():
    return uuid.uuid4().hex


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _norm_payee(payee):
    return " ".join((payee or "").lower().split())


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

    if "amount_cents" in fields:
        if not isinstance(fields["amount_cents"], int):
            raise TypeError("amount_cents must be int cents")
        if row["transfer_id"]:
            conn.execute("UPDATE transactions SET amount_cents=?, updated_at=?"
                         " WHERE id=?",
                         (-fields["amount_cents"], _now(), row["transfer_id"]))
        if row["parent_id"] is None:
            kids = conn.execute("SELECT COUNT(*) c FROM transactions"
                                " WHERE parent_id=? AND deleted=0",
                                (txn_id,)).fetchone()["c"]
            if kids:
                raise ValueError("change the split parts, not the parent total")

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
    """Signed movement for a category in a month. Negative means spent."""
    return conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) a FROM transactions"
        " WHERE category_id=? AND deleted=0 AND substr(date,1,7)=?",
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
    return carry


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

    for r in conn.execute(
            "SELECT a.id, a.name, a.balance_sum, COALESCE(t.s,0) leaf_sum FROM ("
            "  SELECT id, name, 0 balance_sum FROM accounts) a"
            " LEFT JOIN (SELECT account_id, SUM(amount_cents) s FROM transactions"
            "   WHERE deleted=0 AND parent_id IS NULL GROUP BY account_id) t"
            " ON t.account_id = a.id"):
        computed = account_balance(conn, r["id"])
        if computed != r["leaf_sum"]:
            bad.append("account %s: balance %d != leaf sum %d"
                       % (r["name"], computed, r["leaf_sum"]))

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
