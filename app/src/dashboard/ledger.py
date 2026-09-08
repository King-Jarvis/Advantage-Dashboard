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
    # is_income belongs here: a category filed as spending when it is really
    # income makes the budget believe nothing came in, and there has to be a
    # way to correct that without rebuilding the category.
    allowed = {"name", "group_id", "sort", "hidden", "carryover_negative",
               "is_income"}
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

    # Two other tables point here, and neither is history anyone reads: a
    # soft-deleted transaction is already gone from every view, and an
    # import row is staging for a file that has been committed or reverted
    # long since. Left alone they do not protect anything -- they just make
    # the foreign key refuse the delete, so a category with no live use
    # becomes undeletable for reasons the screen cannot explain.
    conn.execute("UPDATE transactions SET category_id=NULL"
                 " WHERE category_id=? AND deleted=1", (category_id,))
    conn.execute("UPDATE import_rows SET category_id=NULL WHERE category_id=?",
                 (category_id,))
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
                    imported_id=None, source="manual", commit=True,
                    category_source=""):
    """A plain transaction. Negative amount means money out.

    `category_source` records who chose the category, when one is given: a
    committed import carries across whichever step suggested it, so a guess
    never arrives looking like a decision.
    """
    if not isinstance(amount_cents, int):
        raise TypeError("amount_cents must be int (cents), got %r" % type(amount_cents))
    tid = new_id()
    conn.execute(
        "INSERT INTO transactions (id, account_id, date, amount_cents, payee,"
        " payee_norm, notes, category_id, category_source, cleared,"
        " imported_id, source, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, account_id, date, amount_cents, payee, _norm_payee(payee), notes,
         category_id, category_source if category_id else "",
         1 if cleared else 0, imported_id, source, _now(), _now()))
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


def split_transaction(conn, txn_id, parts):
    """Divide an existing transaction across categories.

    `parts` is [(category_id, amount_cents), ...]. The row keeps its id and
    becomes the parent: that matters because the id carries the bank's own
    FITID, and losing it would make the same statement re-import as new
    spending the next time it is uploaded.

    The parts must sum to the original amount. A split is a statement about
    what one payment was for, not an opportunity to change how much it was --
    and a balance that quietly moves because someone was itemising a receipt
    is the kind of error nobody finds until it is months old.
    """
    row = conn.execute("SELECT * FROM transactions WHERE id=? AND deleted=0",
                       (txn_id,)).fetchone()
    if row is None:
        raise KeyError(txn_id)
    if row["transfer_id"]:
        raise ValueError("a transfer moves money between your own accounts, "
                         "so there is nothing to divide across categories")
    if row["parent_id"]:
        raise ValueError("this is already part of a split")
    if not parts:
        raise ValueError("a split needs at least one part")
    if not all(isinstance(a, int) for _, a in parts):
        raise TypeError("split amounts must be int cents")
    total = sum(a for _, a in parts)
    if total != row["amount_cents"]:
        raise ValueError(
            "the parts add up to %s but the transaction is %s"
            % (total / 100, row["amount_cents"] / 100))
    for cid, _ in parts:
        if conn.execute("SELECT 1 FROM categories WHERE id=?",
                        (cid,)).fetchone() is None:
            raise KeyError(cid)

    # Re-splitting replaces the previous division rather than adding to it.
    conn.execute("DELETE FROM transactions WHERE parent_id=?", (txn_id,))
    now = _now()
    # The parent carries no category: the children hold the meaning, and the
    # balance counts the parent only.
    conn.execute("UPDATE transactions SET category_id=NULL, updated_at=?"
                 " WHERE id=?", (now, txn_id))
    for cid, amt in parts:
        conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount_cents,"
            " payee, payee_norm, notes, category_id, cleared, parent_id,"
            " source, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id(), row["account_id"], row["date"], amt, row["payee"],
             _norm_payee(row["payee"]), "", cid, row["cleared"], txn_id,
             row["source"], now, now))
    _audit(conn, "update", "split", txn_id, "%d parts" % len(parts))
    conn.commit()
    return txn_id


def unsplit_transaction(conn, txn_id):
    """Put a split back together, leaving the parent uncategorised."""
    row = conn.execute("SELECT id FROM transactions WHERE id=? AND deleted=0",
                       (txn_id,)).fetchone()
    if row is None:
        raise KeyError(txn_id)
    n = conn.execute("DELETE FROM transactions WHERE parent_id=?",
                     (txn_id,)).rowcount
    if n:
        _audit(conn, "update", "split", txn_id, "unsplit")
    conn.commit()
    return n


def split_parts(conn, txn_id):
    """The children of a split, if it has any."""
    return [dict(r) for r in conn.execute(
        "SELECT t.id, t.amount_cents, t.category_id, c.name category"
        " FROM transactions t LEFT JOIN categories c ON c.id = t.category_id"
        " WHERE t.parent_id=? AND t.deleted=0 ORDER BY t.rowid", (txn_id,))]


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
               "cleared", "reconciled", "account_id", "category_source"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError("cannot update: %s" % ", ".join(sorted(bad)))

    # A category set through this function is a decision someone made, unless
    # the caller says which machine step made it. Recording that is what lets
    # a correction outrank the twenty guesses it corrected, instead of being
    # one more vote among them.
    if "category_id" in fields and "category_source" not in fields:
        fields["category_source"] = "you" if fields["category_id"] else ""

    row = conn.execute("SELECT * FROM transactions WHERE id=? AND deleted=0",
                       (txn_id,)).fetchone()
    if row is None:
        raise KeyError(txn_id)
    if row["transfer_id"] and "category_id" in fields and fields["category_id"]:
        raise ValueError("a transfer cannot be categorised")
    if "account_id" in fields:
        # A statement row filed against the wrong account is the reason this
        # is editable at all. The two shapes it must not break: a transfer
        # would end up with both halves in one account, and a split's children
        # must stay with their parent -- an invariant checked at teardown.
        if row["transfer_id"]:
            raise ValueError("unlink the transfer first, then move each half")
        if row["parent_id"]:
            raise ValueError("move the whole split, not one part of it")
        if conn.execute("SELECT 1 FROM accounts WHERE id=?",
                        (fields["account_id"],)).fetchone() is None:
            raise KeyError(fields["account_id"])
    if row["reconciled"] and ("amount_cents" in fields or "date" in fields):
        raise ValueError("reconciled transactions cannot change amount or date")

    has_children = conn.execute(
        "SELECT COUNT(*) c FROM transactions WHERE parent_id=? AND deleted=0",
        (txn_id,)).fetchone()["c"] > 0

    if "account_id" in fields and has_children:
        # Children follow the parent, or the split spans two accounts and the
        # money stops adding up in both.
        conn.execute("UPDATE transactions SET account_id=?, updated_at=?"
                     " WHERE parent_id=?",
                     (fields["account_id"], _now(), txn_id))

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


# A transfer that lands the next day is still the same movement; beyond this
# a coincidence of equal amounts is likelier than a link.
TRANSFER_WINDOW_DAYS = 4


def transfer_candidates(conn, limit=100):
    """Unpaired rows that look like two halves of one movement.

    Matched on the amount being exactly opposite, the accounts differing, and
    the dates being close: a transfer usually clears the sending account
    before the receiving one, so requiring the same date would miss most of
    them.

    Nothing is linked automatically. Two equal and opposite amounts in one
    week can genuinely be a coincidence -- a refund here and a payment there
    -- and silently merging them would delete two real transactions and
    invent a movement that never happened.
    """
    rows = conn.execute(
        "SELECT t.id, t.account_id, t.date, t.amount_cents, t.payee,"
        "       a.name account FROM transactions t"
        " JOIN accounts a ON a.id = t.account_id"
        " WHERE t.deleted=0 AND t.transfer_id IS NULL AND t.parent_id IS NULL"
        " ORDER BY t.date").fetchall()
    outs = [r for r in rows if r["amount_cents"] < 0]
    ins = [r for r in rows if r["amount_cents"] > 0]

    used, pairs = set(), []
    for o in outs:
        if o["id"] in used:
            continue
        for i in ins:
            if i["id"] in used or i["account_id"] == o["account_id"]:
                continue
            if i["amount_cents"] != -o["amount_cents"]:
                continue
            gap = abs((_date(i["date"]) - _date(o["date"])).days)
            if gap > TRANSFER_WINDOW_DAYS:
                continue
            used.add(o["id"])
            used.add(i["id"])
            pairs.append({
                "out_id": o["id"], "in_id": i["id"],
                "amount_cents": -o["amount_cents"],
                "from_account": o["account"], "to_account": i["account"],
                "out_payee": o["payee"], "in_payee": i["payee"],
                "date": o["date"], "days_apart": gap,
            })
            break
        if len(pairs) >= limit:
            break
    return pairs


def _date(text):
    import datetime
    return datetime.date.fromisoformat(str(text)[:10])


def link_transfer(conn, out_id, in_id):
    """Join two existing rows into one movement.

    The rows are kept rather than replaced: each carries the bank's own id for
    its side, and a statement re-imported later has to recognise them.
    Categories are cleared, because a transfer is not spending and not income
    -- counting it as either is how a budget comes to believe a payday
    happened every time money crossed between two of your own accounts.
    """
    out = conn.execute("SELECT * FROM transactions WHERE id=? AND deleted=0",
                       (out_id,)).fetchone()
    inn = conn.execute("SELECT * FROM transactions WHERE id=? AND deleted=0",
                       (in_id,)).fetchone()
    if out is None or inn is None:
        raise KeyError(out_id if out is None else in_id)
    if out["transfer_id"] or inn["transfer_id"]:
        raise ValueError("one of these is already part of a transfer")
    if out["parent_id"] or inn["parent_id"]:
        raise ValueError("a split cannot be half of a transfer")
    if out["account_id"] == inn["account_id"]:
        raise ValueError("a transfer moves money between two accounts")
    if out["amount_cents"] + inn["amount_cents"] != 0:
        raise ValueError("the two halves must cancel out")
    if out["amount_cents"] > 0:
        out, inn = inn, out

    now = _now()
    conn.execute("UPDATE transactions SET transfer_id=?, category_id=NULL,"
                 " updated_at=? WHERE id=?", (inn["id"], now, out["id"]))
    conn.execute("UPDATE transactions SET transfer_id=?, category_id=NULL,"
                 " updated_at=? WHERE id=?", (out["id"], now, inn["id"]))
    _audit(conn, "update", "transfer", out["id"],
           "linked %s -> %s" % (out["account_id"], inn["account_id"]))
    conn.commit()
    return out["id"], inn["id"]


def send_to_account(conn, txn_id, account_id):
    """Record where a movement went, when the other side is not imported.

    Money leaving for Venmo, a savings account nobody downloads statements
    for, or another person is a movement, not spending -- but with only one
    statement imported there is no second row to link to. This writes the
    matching half into the named account and links the pair, so the money
    leaves the budget without being counted as spending and net worth still
    adds up.

    The target must be off budget. For an account you do import, the honest
    answer is to import it and link the two real rows: inventing a half here
    would sit alongside the real one when the statement arrives, and the
    account would be permanently double-counted.
    """
    row = conn.execute("SELECT * FROM transactions WHERE id=? AND deleted=0",
                       (txn_id,)).fetchone()
    if row is None:
        raise KeyError(txn_id)
    if row["transfer_id"]:
        raise ValueError("this is already part of a transfer")
    if row["parent_id"]:
        raise ValueError("a split cannot be half of a transfer")
    other = conn.execute("SELECT id, on_budget FROM accounts WHERE id=?",
                         (account_id,)).fetchone()
    if other is None:
        raise KeyError(account_id)
    if other["id"] == row["account_id"]:
        raise ValueError("a transfer moves money between two accounts")
    if other["on_budget"]:
        raise ValueError(
            "that account is budgeted, so import its statement and link the "
            "two real rows -- writing a matching half here would double-count "
            "it when the statement arrives")

    mirror = new_id()
    now = _now()
    conn.execute(
        "INSERT INTO transactions (id, account_id, date, amount_cents, payee,"
        " payee_norm, notes, category_id, cleared, transfer_id, source,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,NULL,?,NULL,?,?,?)",
        (mirror, account_id, row["date"], -row["amount_cents"], row["payee"],
         _norm_payee(row["payee"]), "other half of a transfer", row["cleared"],
         "transfer-mirror", now, now))
    conn.execute("UPDATE transactions SET transfer_id=?, category_id=NULL,"
                 " updated_at=? WHERE id=?", (mirror, now, txn_id))
    conn.execute("UPDATE transactions SET transfer_id=? WHERE id=?",
                 (txn_id, mirror))
    _audit(conn, "create", "transfer", txn_id, "sent to %s" % account_id)
    conn.commit()
    return mirror


def send_payee_to_account(conn, payee_key, account_id):
    """Send every unfiled row for one merchant to the same place.

    Six rows reading "Transfer to Apple Pay" went to Apple Pay, all six of
    them. Deciding that once is the same reasoning as filing a merchant into
    a category once.
    """
    from .text import norm_payee
    rows = conn.execute(
        "SELECT id, payee FROM transactions"
        " WHERE deleted=0 AND transfer_id IS NULL AND parent_id IS NULL"
        "   AND category_id IS NULL").fetchall()
    n = 0
    for r in rows:
        if norm_payee(r["payee"] or "") != payee_key:
            continue
        send_to_account(conn, r["id"], account_id)
        n += 1
    return n


def unlink_transfer(conn, txn_id):
    """Separate a pair back into two ordinary transactions."""
    row = conn.execute("SELECT id, transfer_id FROM transactions"
                       " WHERE id=? AND deleted=0", (txn_id,)).fetchone()
    if row is None:
        raise KeyError(txn_id)
    if not row["transfer_id"]:
        raise ValueError("this is not part of a transfer")
    other = row["transfer_id"]
    now = _now()
    for tid in (row["id"], other):
        conn.execute("UPDATE transactions SET transfer_id=NULL, updated_at=?"
                     " WHERE id=?", (now, tid))
    _audit(conn, "update", "transfer", row["id"], "unlinked")
    conn.commit()
    return [row["id"], other]


def transfers(conn, limit=200):
    """Every linked movement, said as an operation: out of here, into there."""
    rows = conn.execute(
        "SELECT t.id, t.date, t.amount_cents, t.payee, t.transfer_id,"
        "       a.name account, a.on_budget"
        " FROM transactions t JOIN accounts a ON a.id = t.account_id"
        " WHERE t.deleted=0 AND t.transfer_id IS NOT NULL"
        "   AND t.amount_cents < 0"
        " ORDER BY t.date DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        other = conn.execute(
            "SELECT t.date, a.name account, a.on_budget FROM transactions t"
            " JOIN accounts a ON a.id = t.account_id WHERE t.id=?",
            (r["transfer_id"],)).fetchone()
        if other is None:
            continue
        out.append({
            "id": r["id"], "pair_id": r["transfer_id"], "date": r["date"],
            "amount_cents": -r["amount_cents"],
            "from_account": r["account"], "to_account": other["account"],
            "payee": r["payee"],
            # A move to a tracking account leaves the budget; one between two
            # budgeted accounts does not. Worth saying, because the first
            # changes what there is to spend and the second does not.
            "leaves_budget": bool(r["on_budget"]) and not bool(other["on_budget"]),
            "enters_budget": not bool(r["on_budget"]) and bool(other["on_budget"]),
        })
    return out


def ledger_tree(conn, month=None, limit=1500):
    """Every transaction, arranged the way it is thought about.

    Account, then group, then category, then the rows. That order is not
    arbitrary: it is the order in which a thing is wrong. "That whole file
    went to the wrong account", "those belong under Bills", "that one is not
    groceries" -- each level answers a different mistake.

    Split children are shown and their parents are not: the parent carries no
    category, so it has no place in this arrangement, while its parts do.
    """
    where = ["t.deleted=0"]
    args = []
    if month:
        where.append("substr(t.date,1,7)=?")
        args.append(month)
    rows = conn.execute(
        "SELECT t.id, t.date, t.amount_cents, t.payee, t.category_id,"
        "       t.account_id, t.parent_id, t.transfer_id,"
        "       a.name account, c.name category, c.group_id,"
        "       g.name group_name"
        " FROM transactions t"
        " JOIN accounts a ON a.id = t.account_id"
        " LEFT JOIN categories c ON c.id = t.category_id"
        " LEFT JOIN category_groups g ON g.id = c.group_id"
        " WHERE " + " AND ".join(where) +
        "   AND t.id NOT IN (SELECT parent_id FROM transactions"
        "                    WHERE parent_id IS NOT NULL AND deleted=0)"
        " ORDER BY a.name, g.sort, g.name, c.sort, c.name, t.date DESC"
        " LIMIT ?", [*args, limit]).fetchall()

    tree = {}
    for r in rows:
        acct = tree.setdefault(r["account_id"], {
            "id": r["account_id"], "name": r["account"], "groups": {}})
        # Uncategorised and transfers have no group or category of their own,
        # and hiding them here would make the screen disagree with the ledger.
        gid = r["group_id"] or ("__transfer" if r["transfer_id"] else "__none")
        gname = r["group_name"] or (
            "Transfers" if r["transfer_id"] else "No category")
        grp = acct["groups"].setdefault(gid, {
            "id": gid, "name": gname, "categories": {}})
        cid = r["category_id"] or gid
        cname = r["category"] or gname
        cat = grp["categories"].setdefault(cid, {
            "id": r["category_id"], "name": cname, "rows": []})
        cat["rows"].append({
            "id": r["id"], "date": r["date"], "payee": r["payee"],
            "amount_cents": r["amount_cents"],
            "category_id": r["category_id"], "account_id": r["account_id"],
            "is_transfer": bool(r["transfer_id"]),
            "is_split_part": bool(r["parent_id"]),
        })

    def listify(node, key):
        node[key] = sorted(node[key].values(), key=lambda x: x["name"])
        return node

    out = []
    for acct in tree.values():
        for grp in acct["groups"].values():
            listify(grp, "categories")
        out.append(listify(acct, "groups"))
    return sorted(out, key=lambda a: a["name"])


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


def categorise_payee(conn, payee_key, category_id, overwrite=False,
                     source="you"):
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
        conn.execute(
            "UPDATE transactions SET category_id=?, category_source=?"
            " WHERE id=?", (category_id, source, r["id"]))
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
