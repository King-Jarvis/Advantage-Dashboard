"""What a category could cost, as against what it will.

`stats.py` answers two questions already: what this normally costs, and what
next month is heading for. Neither is a target. Both describe what happens if
nothing changes, which is the right basis for a forecast and no use at all
for deciding to spend less.

This is the third number -- the one to aim for -- and two ideas produce it.

**A target has to be a number you have already hit.** It is a percentile of
your own months, so it is not an aspiration: your statements prove you can
live on it, because in at least one month you did. "Cut eating out by 30%" is
a wish. "Eating out was £151 in June" is evidence.

**Some categories cannot be cut, and pretending otherwise ruins the rest.**
Rent, a loan payment and an insurance premium do not care what you budget.
Shave 20% off everything and you are wrong about the contractual half, and
once one figure is obviously silly the others stop being read. So each
category carries a *flexibility*, and only the flexible ones move; a fixed
cost is budgeted at what it will actually be.

There was a screen built on this. It has been removed -- the numbers were
worth keeping, the screen was not -- so what remains feeds the "aim for"
figure on the budget screen and nothing else. The arithmetic is entirely
deterministic: same months and same flexibility, same answer, every time.
"""

from . import stats

# What a category's flexibility means, and where the target lands.
#
# The percentile is deliberately gentle. p25 is not "the cheapest month you
# ever had" -- it is the level you were at or below a quarter of the time, so
# hitting it means repeating something ordinary rather than achieving
# something exceptional.
FLEXIBILITY = {
    "essential": {
        "label": "Cannot be cut",
        "percentile": None,
        "why": "Contractual or unavoidable. Budgeting less does not pay less.",
    },
    "semi": {
        "label": "Some room",
        "percentile": 40,
        "why": "You need this, but how much you spend on it is partly a choice.",
    },
    "discretionary": {
        "label": "Room to cut",
        "percentile": 25,
        "why": "Worth having, but the amount is entirely up to you.",
    },
}
DEFAULT_FLEXIBILITY = "semi"

def flexibility_of(row):
    """The stored flexibility, or the safe default.

    Unknown values fall back rather than raising. A category classified by an
    older version, or edited by hand in the database, should not break the
    whole plan.
    """
    value = (row["flexibility"] or "").strip().lower()
    return value if value in FLEXIBILITY else DEFAULT_FLEXIBILITY


def target_for(analysis, flexibility):
    """What this category could cost, in cents, with the reason.

    Returns (target_cents, reason). The target is never above what the
    category normally costs -- a savings plan that raises a budget is not a
    savings plan -- and never below a figure you have actually spent.
    """
    expected = analysis.get("suggested_cents") or 0
    if expected <= 0:
        return expected, "nothing spent here"

    # What next month is heading for. A target above that is not a target --
    # it tells you to spend more than you were going to, which is how a
    # saving of minus seven pounds ends up on screen.
    forecast = analysis.get("estimate_cents") or expected
    ceiling = min(expected, forecast) if forecast > 0 else expected

    rule = FLEXIBILITY.get(flexibility, FLEXIBILITY[DEFAULT_FLEXIBILITY])
    if rule["percentile"] is None:
        # You cannot decide to pay less rent, so the number to aim for is
        # what it is actually going to cost -- the forecast, not the average
        # of months that are already behind you.
        return (forecast or expected), "fixed cost, budget what it will be"

    # An annual or occasional charge is not overspending, and trimming it
    # only moves the shortfall to the month the bill arrives.
    if analysis.get("kind") == "sinking":
        return expected, "occasional cost, left alone"

    values = sorted(analysis.get("spend_by_month", {}).values())
    if not values:
        return expected, "no monthly history"

    target = stats.percentile(values, rule["percentile"])

    # A target of zero is a ban, not a budget: the category is overspent the
    # moment you use it once. Anything you genuinely spend on gets a floor at
    # the smallest month you actually had.
    nonzero = [v for v in values if v > 0]
    if nonzero:
        target = max(target, min(nonzero))

    target = min(target, ceiling)
    if target >= ceiling:
        return ceiling, "already at your lowest"
    return target, "you spent this or less in %d of %d months" % (
        sum(1 for v in values if v <= target), len(values))


def targets(conn, end_month=None, window=stats.WINDOW_MONTHS, analyses=None):
    """{category_id: (target_cents, reason)} -- the plan's numbers alone.

    Split out so a caller that has already analysed the ledger does not pay
    for it twice; the overview needs both the forecast and the target on the
    same screen.
    """
    if analyses is None:
        analyses = {a["category_id"]: a for a in
                    stats.analyse_all(conn, end_month=end_month, window=window)}
    rows = conn.execute(
        "SELECT id, flexibility FROM categories"
        " WHERE hidden=0 AND is_income=0").fetchall()
    out = {}
    for row in rows:
        a = analyses.get(row["id"])
        if a is None:
            continue
        out[row["id"]] = target_for(a, flexibility_of(row))
    return out



def set_flexibility(conn, category_id, value, source="you"):
    """Record how movable a category is. Yours outranks the model's."""
    value = (value or "").strip().lower()
    if value not in FLEXIBILITY:
        raise ValueError("flexibility must be one of: %s"
                         % ", ".join(sorted(FLEXIBILITY)))
    if source not in ("you", "model"):
        raise ValueError("source must be 'you' or 'model'")
    cur = conn.execute("SELECT flexibility_source FROM categories WHERE id=?",
                       (category_id,)).fetchone()
    if cur is None:
        raise KeyError(category_id)
    if source == "model" and cur["flexibility_source"] == "you":
        return False
    conn.execute("UPDATE categories SET flexibility=?, flexibility_source=?"
                 " WHERE id=?", (value, source, category_id))
    conn.commit()
    return True
