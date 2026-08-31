"""Money is integer cents. This module exists so that is never in doubt.

Floating point cannot represent 0.10, and a ledger that is wrong by a cent per
transaction is worse than one that is obviously broken, because it is trusted.
Parsing happens once, at the edge, and everything inside works in integers.
"""

import re
from decimal import Decimal, InvalidOperation

_CLEAN = re.compile(r"[^\d\-+.,()]")


def to_cents(value):
    """Parse a money value from a statement into integer cents.

    Handles what real bank exports actually contain: currency symbols, thousand
    separators, both decimal conventions, and parenthesised negatives.

    Decimal, not float: float("1.115") * 100 is 111.49999999999999, and
    round() on that gives 111 rather than 112.
    """
    if value is None or value == "":
        raise ValueError("empty money value")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise TypeError("refusing to convert float to cents: pass a string or int")

    s = str(value).strip()
    negative = s.startswith("(") and s.endswith(")")   # (1.23) means -1.23
    s = _CLEAN.sub("", s).strip("()")

    if "," in s and "." in s:
        # Whichever appears last is the decimal separator: 1.234,56 vs 1,234.56
        s = s.replace(",", "") if s.rindex(".") > s.rindex(",") else \
            s.replace(".", "").replace(",", ".")
    elif "," in s:
        # A lone comma is decimal only when it looks like one: 1,50 not 1,500
        head, _, tail = s.rpartition(",")
        s = head + "." + tail if len(tail) in (1, 2) else s.replace(",", "")

    if s in ("", "-", "+", "."):
        raise ValueError("unparseable money value: %r" % value)
    try:
        d = Decimal(s)
    except InvalidOperation:
        # `from None`: the Decimal internals are noise to a caller who just
        # wants to know which cell of their statement failed to parse.
        raise ValueError("unparseable money value: %r" % value) from None

    cents = int((d * 100).to_integral_value(rounding="ROUND_HALF_UP"))
    return -cents if negative else cents


def fmt(cents, symbol=""):
    """Render cents for display. Never used for storage or arithmetic."""
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(int(cents)), 100)
    return "%s%s%s.%02d" % (sign, symbol, f"{whole:,}", frac)
