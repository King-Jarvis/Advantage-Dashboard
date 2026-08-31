"""Text normalisation shared by the ledger and the importer.

This lives on its own because both need exactly the same answer. When the
ledger stored one normalisation and the importer computed another, duplicate
detection silently stopped working: a re-imported statement looked like new
spending, and the only symptom was a budget that drifted.
"""

import re

_WS = re.compile(r"\s+")
# Words banks add that identify the mechanism rather than the merchant.
_NOISE = re.compile(r"\b(card|visa|debit|pos|purchase|payment|ref|txn|trans)\b")


def norm_payee(text):
    """A comparable form of a payee.

    Banks decorate the same merchant differently on every line -- card
    fragments, store ids, embedded dates. This strips enough of that for
    duplicate detection and per-payee rules to work, while the original is
    kept for display.
    """
    s = (text or "").lower()
    s = re.sub(r"[0-9]{4,}", " ", s)          # card fragments, long ids
    s = re.sub(r"[^a-z0-9&' ]+", " ", s)
    s = _NOISE.sub(" ", s)
    return _WS.sub(" ", s).strip()
