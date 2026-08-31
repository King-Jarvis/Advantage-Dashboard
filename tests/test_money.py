"""Parsing what bank exports actually contain, and never losing a cent."""

import pytest

from dashboard.money import fmt, to_cents


@pytest.mark.parametrize("raw,cents", [
    ("0", 0), ("0.00", 0), ("1", 100), ("1.5", 150), ("1.50", 150),
    ("12.34", 1234), ("-12.34", -1234), ("+12.34", 1234),
    ("$1,234.56", 123456), ("£99.99", 9999), ("12.34 USD", 1234),
    ("(45.00)", -4500), ("(1,234.56)", -123456),
    ("1.234,56", 123456),          # European: dot thousands, comma decimal
    ("1,234.56", 123456),          # Anglo:    comma thousands, dot decimal
    ("1,50", 150),                 # lone comma, 2dp -> decimal
    ("1,5", 150),                  # lone comma, 1dp -> decimal
    ("1,500", 150000),             # lone comma, 3dp -> thousands separator
    ("1,234,567.89", 123456789),
    ("  42  ", 4200),
    (0, 0), (-1234, -1234),        # ints pass through as cents already
])
def test_parses(raw, cents):
    assert to_cents(raw) == cents


@pytest.mark.parametrize("raw,cents", [
    ("1.115", 112),   # half-up; float arithmetic gives 111 here
    ("1.125", 113),
    ("2.675", 268),   # the canonical float-rounding counterexample
    ("0.005", 1),
    ("0.004", 0),
    ("-1.115", -112),
])
def test_rounds_half_up_not_however_floats_feel(raw, cents):
    assert to_cents(raw) == cents


@pytest.mark.parametrize("bad", ["", "   ", None, "abc", "-", ".", "$",
                                 "1.2.3", "..", "1-2"])
def test_rejects_unparseable(bad):
    with pytest.raises(ValueError):
        to_cents(bad)


@pytest.mark.parametrize("bad", [1.0, 12.34, -0.5])
def test_rejects_floats_outright(bad):
    # Accepting a float would mean a fraction of a cent entered the system
    # before we ever saw it.
    with pytest.raises(TypeError):
        to_cents(bad)


def test_large_amounts_stay_exact(t=None):
    # Beyond 2^53, where a float would start losing integers entirely.
    assert to_cents("99999999999999.99") == 9999999999999999


def test_round_trip_through_fmt_is_stable():
    for cents in (0, 1, -1, 99, 100, -123456, 123456789):
        assert to_cents(fmt(cents)) == cents


@pytest.mark.parametrize("cents,shown", [
    (0, "0.00"), (5, "0.05"), (-5, "-0.05"), (100, "1.00"),
    (123456, "1,234.56"), (-123456, "-1,234.56"), (None, "—"),
])
def test_formats(cents, shown):
    assert fmt(cents) == shown
