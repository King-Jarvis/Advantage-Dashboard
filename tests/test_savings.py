"""The savings plan.

Every expected figure is worked out by hand. The point of this component is
that a target is a number the person has already hit, so a test that merely
agrees with the implementation would prove nothing about the only property
that matters.
"""

import pytest

from dashboard import categorize, ledger, savings, stats


@pytest.fixture
def book(conn):
    g = ledger.create_category_group(conn, "Everyday")
    b = ledger.create_category_group(conn, "Bills")
    return {
        "acct": ledger.create_account(conn, "Checking"),
        "dining": ledger.create_category(conn, g, "Eating Out"),
        "food": ledger.create_category(conn, g, "Groceries"),
        "rent": ledger.create_category(conn, b, "Rent"),
        "annual": ledger.create_category(conn, b, "Car Tax"),
    }


def cover(conn, book, ms):
    for m in ms:
        conn.execute(
            "INSERT OR REPLACE INTO month_coverage (account_id, month,"
            " txn_count, first_day, last_day) VALUES (?,?,?,?,?)",
            (book["acct"], m, 1, m + "-01", m + "-28"))
    conn.commit()


def spend(conn, book, cat, month, pounds):
    if pounds:
        ledger.add_transaction(conn, book["acct"], "%s-10" % month,
                               -round(pounds * 100), "Shop", book[cat])


def history(conn, book, cat, amounts, end="2025-08"):
    ms = stats.month_range(end, len(amounts))
    cover(conn, book, ms)
    for m, a in zip(ms, amounts, strict=True):
        spend(conn, book, cat, m, a)
    return ms


# ── the floor is a month you actually had ─────────────────────────────────
def test_a_target_is_a_month_you_have_already_lived_on(conn, book):
    history(conn, book, "dining", [100, 200, 300, 400])
    savings.set_flexibility(conn, book["dining"], "discretionary")

    a = stats.analyse(conn, book["dining"], end_month="2025-08")
    target, reason = savings.target_for(a, "discretionary")

    # Four values, p25 of [100,200,300,400] is 175. Above the £100 month.
    assert target == 175_00
    assert "you spent this or less" in reason
    assert target <= a["suggested_cents"]


def test_a_target_is_never_zero(conn, book):
    """A zero budget is a ban. The category is overspent on first use."""
    history(conn, book, "dining", [0, 0, 0, 240])
    savings.set_flexibility(conn, book["dining"], "discretionary")

    a = stats.analyse(conn, book["dining"], end_month="2025-08")
    target, _ = savings.target_for(a, "discretionary")
    assert target > 0


def test_a_target_never_rises_above_what_you_spend(conn, book):
    """A plan that raises a budget is not a savings plan.

    A left-skewed history puts the 40th percentile above the trimmed mean,
    so without the clamp 'saving' would hand the category more money.
    """
    history(conn, book, "food", [10, 500, 520, 540])
    savings.set_flexibility(conn, book["food"], "semi")

    a = stats.analyse(conn, book["food"], end_month="2025-08")
    target, _ = savings.target_for(a, "semi")
    assert target <= a["suggested_cents"]


# ── what must not be cut ──────────────────────────────────────────────────
def test_a_fixed_cost_is_never_trimmed(conn, book):
    """Budgeting less for rent does not make the rent smaller.

    It is budgeted at what it is heading for rather than at an average of
    months already behind you -- the average is the wrong number to hold
    against a bill that has since gone up.
    """
    history(conn, book, "rent", [900, 900, 900, 950])
    savings.set_flexibility(conn, book["rent"], "essential")

    a = stats.analyse(conn, book["rent"], end_month="2025-08")
    target, reason = savings.target_for(a, "essential")
    assert target == a["estimate_cents"]
    assert target >= a["median_cents"], "never trimmed below what it costs"
    assert reason == "fixed cost, budget what it will be"


def test_an_occasional_cost_is_left_alone_even_when_flexible(conn, book):
    """Trimming a sinking fund moves the shortfall to the month it arrives."""
    ms = stats.month_range("2025-12", 12)
    cover(conn, book, ms)
    for m in ms[:11]:
        spend(conn, book, "annual", m, 0)
    spend(conn, book, "annual", ms[11], 600)

    a = stats.analyse(conn, book["annual"], end_month="2025-12")
    assert a["kind"] == "sinking"
    target, reason = savings.target_for(a, "discretionary")
    assert target == a["suggested_cents"]
    assert reason == "occasional cost, left alone"


# ── flexibility ───────────────────────────────────────────────────────────
def test_your_judgement_outranks_the_models(conn, book):
    savings.set_flexibility(conn, book["dining"], "essential", source="you")
    changed = savings.set_flexibility(conn, book["dining"], "discretionary",
                                      source="model")
    assert changed is False
    row = conn.execute("SELECT flexibility, flexibility_source FROM categories"
                       " WHERE id=?", (book["dining"],)).fetchone()
    assert row["flexibility"] == "essential"
    assert row["flexibility_source"] == "you"


def test_the_model_may_fill_in_what_you_have_not_judged(conn, book):
    assert savings.set_flexibility(conn, book["dining"], "discretionary",
                                   source="model") is True
    assert savings.set_flexibility(conn, book["dining"], "semi",
                                   source="you") is True


def test_an_invented_class_is_refused(conn, book):
    with pytest.raises(ValueError):
        savings.set_flexibility(conn, book["dining"], "whatever")


def test_an_unclassified_category_defaults_to_cautious(conn, book):
    row = conn.execute("SELECT flexibility FROM categories WHERE id=?",
                       (book["dining"],)).fetchone()
    assert savings.flexibility_of(row) == "semi"


def test_only_unjudged_categories_are_worth_asking_about(conn, book):
    assert len(savings.needs_classifying(conn)) == 4
    savings.set_flexibility(conn, book["rent"], "essential")
    names = [c["name"] for c in savings.needs_classifying(conn)]
    assert "Rent" not in names and len(names) == 3


# ── the whole plan ────────────────────────────────────────────────────────
def test_the_plan_totals_what_it_frees_up(conn, book):
    history(conn, book, "dining", [100, 200, 300, 400])
    history(conn, book, "rent", [900, 900, 900, 900])
    savings.set_flexibility(conn, book["dining"], "discretionary")
    savings.set_flexibility(conn, book["rent"], "essential")

    p = savings.plan(conn, end_month="2025-08")
    by = {c["name"]: c for c in p["categories"]}

    assert by["Rent"]["saves_cents"] == 0
    assert by["Eating Out"]["target_cents"] == 175_00
    dining = by["Eating Out"]
    assert dining["saves_cents"] == dining["expected_cents"] - 175_00
    assert p["saves_cents"] == sum(c["saves_cents"] for c in p["categories"])
    assert p["target_cents"] == p["expected_cents"] - p["saves_cents"]


def test_the_plan_leads_with_the_biggest_win(conn, book):
    history(conn, book, "dining", [100, 200, 300, 400])
    history(conn, book, "food", [10, 20, 30, 40])
    for c in ("dining", "food"):
        savings.set_flexibility(conn, book[c], "discretionary")

    p = savings.plan(conn, end_month="2025-08")
    saves = [c["saves_cents"] for c in p["categories"]]
    assert saves == sorted(saves, reverse=True)


def test_the_plan_says_when_it_has_too_little_history(conn, book):
    history(conn, book, "dining", [100, 200])
    p = savings.plan(conn, end_month="2025-08")
    assert p["enough"] is False
    assert p["minimum"] == stats.MIN_MONTHS


def test_the_plan_names_what_it_has_not_judged(conn, book):
    history(conn, book, "dining", [100, 200, 300, 400])
    p = savings.plan(conn, end_month="2025-08")
    assert "Eating Out" in p["unclassified"]
    savings.set_flexibility(conn, book["dining"], "discretionary")
    assert "Eating Out" not in savings.plan(conn, end_month="2025-08")["unclassified"]


# ── the model call ────────────────────────────────────────────────────────
def test_nothing_is_sent_when_the_model_is_off(conn, book, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the model must not be called when it is off")
    monkeypatch.setattr(savings, "_ask_model", boom)
    out = savings.classify(conn, use_model=False)
    assert out["classified"] == 0
    assert out["asked"] == 0


def test_nothing_is_sent_when_there_is_nothing_to_ask(conn, book, monkeypatch):
    """Pressing the button again with nothing new must cost nothing."""
    for c in ("dining", "food", "rent", "annual"):
        savings.set_flexibility(conn, book[c], "semi")

    def boom(*a, **k):
        raise AssertionError("asked the model about an already-sorted category")

    monkeypatch.setattr(savings, "_ask_model", boom)
    monkeypatch.setattr(categorize, "available", lambda: True)
    out = savings.classify(conn, use_model=True)
    assert out["asked"] == 0
    assert out["classified"] == 0


def test_only_names_are_sent(conn, book, monkeypatch):
    """Not amounts, not dates, not payees. The judgement is about words."""
    history(conn, book, "dining", [100, 200, 300, 400])
    seen = {}

    def fake(names, **kw):
        seen["names"] = list(names)
        return {n: "discretionary" for n in names}

    monkeypatch.setattr(savings, "_ask_model", fake)
    monkeypatch.setattr(categorize, "available", lambda: True)
    savings.classify(conn, use_model=True)
    assert seen["names"] == ["Car Tax", "Eating Out", "Groceries", "Rent"]


def test_an_invented_class_from_the_model_is_discarded(conn, book, monkeypatch):
    monkeypatch.setattr(savings, "_ask_model",
                        lambda names, **kw: {"Rent": "obviously_free"})
    monkeypatch.setattr(categorize, "available", lambda: True)
    out = savings.classify(conn, use_model=True)
    assert out["classified"] == 0
    row = conn.execute("SELECT flexibility FROM categories WHERE id=?",
                       (book["rent"],)).fetchone()
    assert row["flexibility"] == ""


# ── the two figures must not be the same number twice ─────────────────────
def test_the_target_is_not_just_the_forecast_again(conn, book):
    """They answer different questions and used to answer with one number.

    Estimate: what next month costs if nothing changes. Target: what to aim
    for instead. A screen showing the same figure under both labels is
    telling you to spend exactly what you were going to.
    """
    history(conn, book, "dining", [100, 200, 300, 400])
    savings.set_flexibility(conn, book["dining"], "discretionary")

    a = stats.analyse(conn, book["dining"], end_month="2025-08")
    target, _ = savings.target_for(a, "discretionary")
    assert target < a["estimate_cents"], "a target must ask for less"


def test_a_rising_habit_widens_the_gap_it_has_to_close(conn, book):
    """The worse the trend, the further the target sits below the forecast --
    which is the number that should be alarming."""
    history(conn, book, "dining", [100, 150, 200, 900])
    savings.set_flexibility(conn, book["dining"], "discretionary")
    a = stats.analyse(conn, book["dining"], end_month="2025-08")
    target, _ = savings.target_for(a, "discretionary")
    assert a["estimate_cents"] > a["trimmed_mean_cents"]
    assert target < a["trimmed_mean_cents"]


def test_a_rising_fixed_cost_is_budgeted_at_the_higher_figure(conn, book):
    """The safe error for a bill you cannot argue with is to over-provide."""
    history(conn, book, "rent", [800, 900, 1000, 1100])
    savings.set_flexibility(conn, book["rent"], "essential")
    a = stats.analyse(conn, book["rent"], end_month="2025-08")
    target, _ = savings.target_for(a, "essential")
    assert target > a["trimmed_mean_cents"]
    assert target == a["estimate_cents"]


def test_a_target_never_asks_you_to_spend_more_than_you_were_going_to(conn, book):
    """A category settling down had a "target" above its own forecast.

    The percentile came from months that are already behind you; the forecast
    knows the last of them was cheaper. Aiming at the older, higher figure is
    not a saving, and it showed on screen as one -- negative.
    """
    history(conn, book, "dining", [900, 900, 400])
    savings.set_flexibility(conn, book["dining"], "semi")
    a = stats.analyse(conn, book["dining"], end_month="2025-08")
    target, _ = savings.target_for(a, "semi")
    assert target <= a["estimate_cents"]


def test_a_fixed_cost_is_budgeted_at_what_it_will_actually_be(conn, book):
    """You cannot decide to pay less rent, so the number to aim for is the
    one it is heading towards -- not an average of months already gone."""
    history(conn, book, "rent", [800, 900, 1000])
    savings.set_flexibility(conn, book["rent"], "essential")
    a = stats.analyse(conn, book["rent"], end_month="2025-08")
    target, reason = savings.target_for(a, "essential")
    assert target == a["estimate_cents"]
    assert reason == "fixed cost, budget what it will be"


def test_no_category_ever_shows_a_negative_saving(conn, book):
    for cat, amounts in (("dining", [900, 900, 400]),
                         ("food", [10, 500, 520]),
                         ("rent", [800, 900, 1000]),
                         ("annual", [0, 0, 600])):
        history(conn, book, cat, amounts)
    p = savings.plan(conn, end_month="2025-08")
    for c in p["categories"]:
        assert c["saves_cents"] >= 0, f"{c['name']} saves {c['saves_cents']}"
