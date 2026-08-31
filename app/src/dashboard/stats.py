"""What your own history says a category costs.

Every figure here is computed from your transactions with ordinary
statistics. No model is involved. That is not a cost saving: a language model
is unreliable at arithmetic over hundreds of rows, and a budget you cannot
reproduce or audit is worse than no budget. Run this twice on the same data
and you get the same answer, which is the whole point.

The rule that matters most is that a month with no statement behind it is not
a month with no spending. Averaging over a gap drags the mean toward zero and
produces a confident, wrong recommendation. Every figure is computed over
*covered* months only, and the number of them travels with the result so the
interface can be honest about how much it actually knows.
"""

import statistics
from datetime import date

WINDOW_MONTHS = 12
TRIM_PCT = 10
MIN_MONTHS = 3          # below this the engine declines rather than guesses

# A category whose monthly totals vary by less than this is treated as fixed:
# rent and insurance rather than groceries.
FIXED_CV = 0.08
# Appearing in at most this many months of a full year, while still costing
# real money, is an annual or occasional charge -- the sort a monthly average
# hides until it arrives.
SINKING_MAX_ACTIVE = 3


def month_key(d):
    return d[:7]


def prev_month(m):
    y, mm = int(m[:4]), int(m[5:7])
    return "%04d-%02d" % (y - 1, 12) if mm == 1 else "%04d-%02d" % (y, mm - 1)


def month_range(end_month, count):
    """The `count` months ending at `end_month`, oldest first."""
    out, m = [], end_month
    for _ in range(count):
        out.append(m)
        m = prev_month(m)
    return list(reversed(out))


def this_month():
    t = date.today()
    return "%04d-%02d" % (t.year, t.month)


# ── coverage ──────────────────────────────────────────────────────────────
def covered_months(conn, end_month=None, window=WINDOW_MONTHS):
    """Months in the window that actually have data behind them.

    Consults month_coverage, which the importer maintains. A month absent
    from it is a month we have no statement for -- not a month in which
    nothing was spent.
    """
    end_month = end_month or this_month()
    wanted = set(month_range(end_month, window))
    rows = conn.execute(
        "SELECT DISTINCT month FROM month_coverage ORDER BY month").fetchall()
    have = {r["month"] for r in rows}
    if not have:
        # No import has happened. Fall back to months the ledger itself has,
        # so a hand-entered book still gets recommendations.
        rows = conn.execute(
            "SELECT DISTINCT substr(date,1,7) m FROM transactions"
            " WHERE deleted=0").fetchall()
        have = {r["m"] for r in rows}
    return sorted(wanted & have)


def monthly_spend(conn, category_id, months):
    """Positive spend per month, in cents. Only the months asked for.

    Income and refunds net off within a month, which is what you want: a
    return reduces what that month cost you.
    """
    if not months:
        return {}
    marks = ",".join("?" * len(months))
    rows = conn.execute(
        "SELECT substr(t.date,1,7) m, SUM(t.amount_cents) total"
        " FROM transactions t JOIN accounts a ON a.id = t.account_id"
        " WHERE t.category_id=? AND t.deleted=0 AND a.on_budget=1"
        "   AND substr(t.date,1,7) IN (%s) GROUP BY 1" % marks,
        [category_id, *months]).fetchall()
    found = {r["m"]: -r["total"] for r in rows}
    # A covered month with no transactions really did cost nothing.
    return {m: found.get(m, 0) for m in months}


# ── summary statistics ────────────────────────────────────────────────────
def trimmed_mean(values, pct=TRIM_PCT):
    """Mean with the extremes dropped.

    One car repair should not permanently inflate an auto envelope. Trimming
    is symmetric so an unusually cheap month is discounted too.
    """
    if not values:
        return 0
    ordered = sorted(values)
    k = int(len(ordered) * pct / 100)
    # A strict percentage trims nothing below ten values, which is precisely
    # when a single outlier does the most damage -- a six-month window with
    # one car repair in it. Once there are enough values to spare a pair,
    # always drop at least one from each end.
    if len(ordered) >= 5:
        k = max(1, k)
    kept = ordered[k:len(ordered) - k] or ordered
    return round(statistics.fmean(kept))


def mad(values):
    """Median absolute deviation: spread that a single outlier cannot inflate."""
    if len(values) < 2:
        return 0
    med = statistics.median(values)
    return round(statistics.median([abs(v - med) for v in values]))


def percentile(values, p):
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * p / 100
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * frac)


def trend_pct(values):
    """Change from the first half of the window to the second, as a percent."""
    if len(values) < 4:
        return 0
    half = len(values) // 2
    a = statistics.fmean(values[:half])
    b = statistics.fmean(values[len(values) - half:])
    if a == 0:
        return 0
    return round((b - a) / a * 100)


# ── classification ────────────────────────────────────────────────────────
def classify(spend_by_month):
    """Decide what kind of cost this is.

    The class is what makes the suggestion useful. An annual insurance
    premium averaged across twelve months disappears into noise; named as a
    sinking fund it becomes "set aside this much each month", which is the
    advice that actually prevents the surprise.
    """
    months = sorted(spend_by_month)
    values = [spend_by_month[m] for m in months]
    active = [v for v in values if v > 0]

    if len(months) < MIN_MONTHS:
        return "insufficient"
    if not active:
        return "unused"
    if len(active) <= SINKING_MAX_ACTIVE and len(months) >= 6:
        # Including a single occurrence. The engine cannot know whether a
        # charge will return, but the cost of being wrong is lopsided:
        # setting money aside for something that never recurs leaves a
        # surplus, while budgeting nothing for something annual leaves you
        # short by the whole amount. So it saves, and says how many times it
        # has actually seen the charge -- marking it a one-off is the
        # operator's call, not a guess made on their behalf.
        return "sinking"

    mean = statistics.fmean(active)
    if mean > 0:
        cv = (statistics.pstdev(active) / mean) if len(active) > 1 else 0
        # Present in nearly every month and barely varying: a standing cost.
        if cv <= FIXED_CV and len(active) >= len(months) - 1:
            return "fixed"
    return "variable"


def confidence(months_covered, kind, values):
    """How much this recommendation should be trusted, stated plainly."""
    if kind in ("insufficient", "unused"):
        return "none"
    if months_covered >= 9:
        base = "high"
    elif months_covered >= 6:
        base = "medium"
    else:
        base = "low"
    # Wide spread undermines an otherwise well-sampled category.
    active = [v for v in values if v > 0]
    if base == "high" and len(active) > 2:
        med = statistics.median(active)
        if med and mad(active) / med > 0.5:
            base = "medium"
    return base


def analyse(conn, category_id, end_month=None, window=WINDOW_MONTHS):
    """Everything known about one category's spending."""
    months = covered_months(conn, end_month, window)
    spend = monthly_spend(conn, category_id, months)
    values = [spend[m] for m in months]
    active = [v for v in values if v > 0]
    kind = classify(spend)

    out = {
        "category_id": category_id,
        "months": months,
        "sample_months": len(months),
        "spend_by_month": spend,
        "kind": kind,
        "confidence": confidence(len(months), kind, values),
        "trend_pct": trend_pct(values),
        "median_cents": int(statistics.median(values)) if values else 0,
        "trimmed_mean_cents": trimmed_mean(values),
        "p25_cents": percentile(values, 25),
        "p75_cents": percentile(values, 75),
        "mad_cents": mad(values),
        "excluded": [],
        "occurrences": len([v for v in values if v > 0]),
        "suggested_cents": None,
        "low_cents": None,
        "high_cents": None,
        "basis": "",
    }

    if kind == "insufficient":
        out["basis"] = ("only %d covered month%s; needs at least %d"
                        % (len(months), "" if len(months) == 1 else "s",
                           MIN_MONTHS))
        return out

    if kind == "unused":
        # No evidence, so no opinion. Asserting zero would read as "budget
        # nothing here", which is wrong for a savings goal that has simply
        # not been drawn on yet -- and the engine cannot tell a goal from a
        # dead category. Declining is the safer of the two errors.
        out["suggested_cents"] = None
        out["basis"] = "nothing spent in %d covered months; no recommendation" \
                       % len(months)
        return out

    if kind == "sinking":
        total = sum(active)
        per_month = round(total / max(1, len(months)))
        out["suggested_cents"] = per_month
        out["low_cents"] = per_month
        out["high_cents"] = per_month
        out["occurrences"] = len(active)
        out["excluded"] = [m for m, v in spend.items() if v > 0]
        once = len(active) == 1
        out["basis"] = (
            "%s seen %s across %d months, set aside monthly%s"
            % (_fmt(total), "once" if once else "%d times" % len(active),
               len(months),
               "; mark it a one-off if it will not return" if once else ""))
        return out

    if kind == "fixed":
        typical = int(statistics.median(active))
        out["suggested_cents"] = typical
        out["low_cents"] = min(active)
        out["high_cents"] = max(active)
        out["basis"] = "the same amount most months"
        return out

    # variable
    out["suggested_cents"] = out["trimmed_mean_cents"]
    out["low_cents"] = out["p25_cents"]
    out["high_cents"] = out["p75_cents"]
    ordered = sorted(values)
    k = int(len(ordered) * TRIM_PCT / 100)
    if k:
        trimmed = set(ordered[:k]) | set(ordered[len(ordered) - k:])
        out["excluded"] = [m for m in months if spend[m] in trimmed]
    out["basis"] = "typical of the last %d months" % len(months)
    return out


def _fmt(cents):
    return "%d.%02d" % divmod(abs(int(cents)), 100)


def analyse_all(conn, end_month=None, window=WINDOW_MONTHS):
    """Every budgetable category, most significant first."""
    cats = conn.execute(
        "SELECT c.id, c.name, g.name gname FROM categories c"
        " JOIN category_groups g ON g.id = c.group_id"
        " WHERE c.is_income=0 AND c.hidden=0 ORDER BY g.sort, c.sort"
    ).fetchall()
    out = []
    for c in cats:
        a = analyse(conn, c["id"], end_month, window)
        a["name"] = c["name"]
        a["group"] = c["gname"]
        out.append(a)
    return out


def totals(conn, end_month=None, window=WINDOW_MONTHS):
    """What a full month would cost if every suggestion were accepted."""
    rows = analyse_all(conn, end_month, window)
    return {
        "suggested_total_cents": sum(r["suggested_cents"] or 0 for r in rows),
        "categories": len(rows),
        "with_suggestion": sum(1 for r in rows if r["suggested_cents"] is not None),
        "insufficient": sum(1 for r in rows if r["kind"] == "insufficient"),
        "months": rows[0]["sample_months"] if rows else 0,
    }
