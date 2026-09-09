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

# How fast older months stop counting towards what next month will cost.
# Three months, so a month a quarter old carries half the weight of the one
# just gone. Slow enough that one heavy month does not become the forecast,
# fast enough that a genuine change in habit shows up while it is still
# useful to know.
RECENCY_HALF_LIFE = 3.0


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
def _coverage_by_month(conn):
    """{month: set of on-budget accounts with a statement for it}.

    Off-budget accounts are excluded here for the same reason their spending
    is: they are outside the budget, so whether their statement was imported
    says nothing about whether a month is comparable.
    """
    rows = conn.execute(
        "SELECT c.month, c.account_id FROM month_coverage c"
        " JOIN accounts a ON a.id = c.account_id"
        " WHERE a.on_budget=1").fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["month"], set()).add(r["account_id"])
    return out


def covered_months(conn, end_month=None, window=WINDOW_MONTHS):
    """Months in the window that can honestly be averaged together.

    Three tests, not one. "Has data behind it" was the only one for a long
    time, and the two missing ones were each worth about half the budget.

    **It has a statement.** A month absent from month_coverage is a month we
    have no statement for -- not a month in which nothing was spent.

    **It has finished.** The month in progress holds however many days have
    elapsed. Averaged in as though it were a whole one it drags every figure
    down, and worst for whatever is billed late in the month. On the seventh
    it is a quarter of a data point pretending to be a whole one.

    **It saw the same accounts as the others.** Importing one statement for
    April and all six for July does not mean April was cheap; it means April
    is a different-sized universe. Mixing them understates every category by
    roughly the ratio of the two, which looks exactly like a frugal spring.

    The reference set is the accounts covering at least half the finished
    months, rather than every account that has ever been covered. Otherwise
    connecting a new account today would disqualify all of your history until
    you went back and imported its statements too.
    """
    end_month = end_month or this_month()
    wanted = set(month_range(end_month, window))
    by_month = _coverage_by_month(conn)

    if not by_month:
        # No import has happened. Fall back to months the ledger itself has,
        # so a hand-entered book still gets recommendations.
        rows = conn.execute(
            "SELECT DISTINCT substr(date,1,7) m FROM transactions"
            " WHERE deleted=0").fetchall()
        return sorted(wanted & {r["m"] for r in rows} - {this_month()})

    finished = sorted((wanted & set(by_month)) - {this_month()})
    if not finished:
        return []

    counts = {}
    for m in finished:
        for acct in by_month[m]:
            counts[acct] = counts.get(acct, 0) + 1
    need = (len(finished) + 1) // 2
    reference = {a for a, n in counts.items() if n >= need}

    return [m for m in finished if reference <= by_month[m]]


def coverage_notes(conn, end_month=None, window=WINDOW_MONTHS):
    """Which months were used, which were dropped, and why.

    The engine declining to average over a month is the right call, but it is
    only defensible if it says so. Silently using three of six months and
    presenting the result as "your history" is how a recommendation becomes
    unarguable.
    """
    end_month = end_month or this_month()
    wanted = set(month_range(end_month, window))
    by_month = _coverage_by_month(conn)
    used = covered_months(conn, end_month, window)
    n_accounts = conn.execute(
        "SELECT COUNT(*) n FROM accounts WHERE on_budget=1").fetchone()["n"]

    dropped = []
    for m in sorted((wanted & set(by_month)) - set(used)):
        if m == this_month():
            why = "still in progress"
        else:
            why = ("only %d of %d accounts imported"
                   % (len(by_month[m]), n_accounts))
        dropped.append({"month": m, "reason": why})

    return {"used": used, "dropped": dropped,
            "enough": len(used) >= MIN_MONTHS, "minimum": MIN_MONTHS}


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
def recency_weighted(months, spend):
    """What next month looks like, given that recent months count for more.

    Distinct from the trimmed mean on purpose, and the distinction is the
    point. The trimmed mean answers "what does this normally cost", which is
    the right basis for a target you mean to hold yourself to -- it is stable,
    and it ignores the extremes rather than chasing them.

    This answers a different question: what is about to happen if nothing
    changes. Spending that has been climbing for three months is not going to
    average itself out next month, and a forecast that says so is no use for
    deciding what to do about it.

    Weights decay by half every RECENCY_HALF_LIFE months. A weighted mean of
    the values always lands between the smallest and largest of them, so this
    cannot project a month that has never happened.
    """
    if not months:
        return 0
    n = len(months)
    total = weight = 0.0
    for i, m in enumerate(months):          # oldest first
        w = 0.5 ** ((n - 1 - i) / RECENCY_HALF_LIFE)
        total += spend.get(m, 0) * w
        weight += w
    return round(total / weight) if weight else 0


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
        # What next month costs if nothing changes, as distinct from what
        # this normally costs. The two used to be the same number wearing
        # two labels, which made the chart's estimate and its recommendation
        # sit on top of each other and say nothing to each other.
        "estimate_cents": recency_weighted(months, spend),
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
        # It declines to recommend below MIN_MONTHS; forecasting from the
        # same evidence would be the same guess wearing a different label.
        out["estimate_cents"] = 0
        out["basis"] = ("only %d covered month%s; needs at least %d"
                        % (len(months), "" if len(months) == 1 else "s",
                           MIN_MONTHS))
        return out

    if kind == "unused":
        out["estimate_cents"] = 0
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
        # A recency weighting over eleven zeroes and one large month forecasts
        # whichever it saw last, which is wrong in both directions. The
        # expected cost of a month is the share, and that is the honest
        # forecast even though no single month will look like it.
        out["estimate_cents"] = per_month
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


def income_by_month(conn, months):
    """Money in, per month, as a positive figure.

    monthly_spend negates: it answers "what did this cost", so income comes
    back below zero. Flipping it here rather than teaching that function two
    meanings, because a function that returns a sign depending on the
    category is one nobody can read a call site of.
    """
    if not months:
        return {}
    marks = ",".join("?" * len(months))
    rows = conn.execute(
        "SELECT substr(t.date,1,7) m, SUM(t.amount_cents) total"
        " FROM transactions t"
        " JOIN accounts a ON a.id = t.account_id"
        " JOIN categories c ON c.id = t.category_id"
        " WHERE t.deleted=0 AND a.on_budget=1 AND c.is_income=1"
        "   AND substr(t.date,1,7) IN (%s) GROUP BY 1" % marks,
        list(months)).fetchall()
    found = {r["m"]: max(0, r["total"]) for r in rows}
    return {m: found.get(m, 0) for m in months}


def income_forecast(conn, end_month=None, window=WINDOW_MONTHS):
    """What a month's income usually is, and what this one is heading for.

    Not routed through analyse(). classify() calls a category active only
    where its monthly figure is positive, and income's are negative by that
    function's convention -- so every income category came back "unused",
    with no forecast at all. Bending the classifier to handle both signs
    would make every branch in it ask which kind it was looking at; income
    only needs the two averages, so it takes them directly.
    """
    months = covered_months(conn, end_month=end_month, window=window)
    by_month = income_by_month(conn, months)
    values = [by_month[m] for m in months]
    return {
        "months": months,
        "by_month": by_month,
        "typical_cents": trimmed_mean(values),
        "estimate_cents": recency_weighted(months, by_month),
        "enough": len(months) >= MIN_MONTHS,
    }


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
