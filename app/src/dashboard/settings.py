"""Dashboard settings, and the connected Google accounts.

Settings are key/value so that adding one is not a migration. Each has a
declared type and default, and unknown keys are refused -- a settings table
that accepts anything becomes a place bugs hide.

Secret values are encrypted and never leave the server. The API reports them
as set or not set, which is all an interface needs to render a field, and
means a compromised session cannot read back the keys.
"""

import json
import time

from . import crypt

# key -> (kind, default, description)
# kind: bool | int | text | choice:a,b,c | secret
SPEC = {
    "currency_symbol":   ("text", "", "Prefix shown before amounts"),
    "month_start_day":   ("int", 1, "Day the budget month begins"),
    "timezone":          ("text", "UTC", "Used for dates and schedules"),

    "baseline_window_months": ("int", 12, "Months of history the engine weighs"),
    "min_months_for_suggestion": ("int", 3,
                                  "Below this it declines rather than guessing"),

    "enable_llm_categories": ("bool", False,
                              "Send unknown merchant names for categorising"),
    "enable_spending_analysis": ("bool", False,
                                 "Send category totals for written analysis"),
    "classify_model":    ("text", "claude-haiku-4-5", "Model used for both"),

    "mail_poll_seconds": ("int", 30, "How often mail is checked"),
    "calendar_poll_seconds": ("int", 30, "How often the calendar is checked"),
    "inbox_min_importance": ("int", 3, "Only show mail scoring at least this"),

    "anthropic_api_key": ("secret", "", "Enables the model-backed features"),
    "google_client_id":  ("text", "", "From your Google Cloud project"),
    "google_client_secret": ("secret", "", "From your Google Cloud project"),
    # Held here rather than in the environment so the whole setup can be done
    # in the browser. It is a secret like any other: written, never returned.
    "ingest_key": ("secret", "", "Shared with n8n so it can trigger a sync"),
}

SECRET_KINDS = {"secret"}

# Written out rather than derived from the key. Title-casing an identifier
# gives "Anthropic Api Key" and "Enable Llm Categories", which reads as
# something nobody looked at.
LABELS = {
    "currency_symbol": "Currency symbol",
    "month_start_day": "Month starts on day",
    "timezone": "Time zone",
    "baseline_window_months": "History window (months)",
    "min_months_for_suggestion": "Minimum months to suggest",
    "enable_llm_categories": "Categorise unknown merchants",
    "enable_spending_analysis": "Written spending analysis",
    "classify_model": "Model",
    "mail_poll_seconds": "Mail check (seconds)",
    "calendar_poll_seconds": "Calendar check (seconds)",
    "inbox_min_importance": "Inbox importance threshold",
    "anthropic_api_key": "Anthropic API key",
    "google_client_id": "Google client ID",
    "google_client_secret": "Google client secret",
    "ingest_key": "n8n ingest key",
}


def kind_of(key):
    return SPEC[key][0] if key in SPEC else None


def _coerce(kind, value):
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if kind == "int":
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValueError("expected a whole number") from None
    return "" if value is None else str(value)


def get(conn, key):
    """A single setting, decoded to its declared type."""
    if key not in SPEC:
        raise KeyError(key)
    kind, default, _ = SPEC[key]
    row = conn.execute("SELECT value, secret FROM settings WHERE key=?",
                       (key,)).fetchone()
    if row is None:
        return default
    if kind in SECRET_KINDS:
        return crypt.decrypt(row["value"]) or ""
    if kind == "bool":
        return row["value"] == "1"
    if kind == "int":
        try:
            return int(row["value"])
        except ValueError:
            return default
    return row["value"]


def set_(conn, key, value):
    """Write one setting. Secrets are encrypted; refuses if it cannot be."""
    if key not in SPEC:
        raise KeyError(key)
    kind, _, _ = SPEC[key]
    if kind in SECRET_KINDS:
        # Never silently store a credential in the clear.
        stored = crypt.encrypt(value) if str(value) else ""
    else:
        coerced = _coerce(kind, value)
        stored = "1" if coerced is True else "0" if coerced is False else str(coerced)
    conn.execute(
        "INSERT INTO settings (key, value, secret, updated_at) VALUES (?,?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
        " secret=excluded.secret, updated_at=excluded.updated_at",
        (key, stored, 1 if kind in SECRET_KINDS else 0,
         time.strftime("%Y-%m-%dT%H:%M:%S")))
    conn.commit()


def clear(conn, key):
    if key not in SPEC:
        raise KeyError(key)
    conn.execute("DELETE FROM settings WHERE key=?", (key,))
    conn.commit()


def all_for_display(conn):
    """Every setting, with secrets reduced to whether they are set.

    A secret is never returned, even to an authenticated session: reading one
    back is not something the interface needs, and not offering it removes a
    way for a stolen session to harvest credentials.
    """
    out = []
    for key, (kind, default, desc) in SPEC.items():
        item = {"key": key, "kind": kind, "description": desc,
                "label": LABELS.get(key, key.replace("_", " ")),
                "default": default}
        if kind in SECRET_KINDS:
            item["is_set"] = bool(get(conn, key))
            item["value"] = None
        else:
            item["value"] = get(conn, key)
        out.append(item)
    return out


def as_dict(conn):
    return {k: get(conn, k) for k in SPEC}


def export_env(conn):
    """Settings the rest of the code reads through os.environ.

    Kept in one place so it is obvious which settings have an environment
    twin, and so a value set in the interface takes effect without a restart.
    """
    return {
        "ENABLE_LLM_CATEGORIES": "true" if get(conn, "enable_llm_categories") else "",
        "CLASSIFY_MODEL": get(conn, "classify_model"),
        "ANTHROPIC_API_KEY": get(conn, "anthropic_api_key"),
    }


# ── connected Google accounts ─────────────────────────────────────────────
def list_google_accounts(conn):
    rows = conn.execute(
        "SELECT id, sub, email, label, scopes, connected_at, last_sync_at,"
        " last_error, expires_at FROM google_accounts ORDER BY connected_at"
    ).fetchall()
    return [dict(r) for r in rows]


def save_google_account(conn, sub, email, refresh_token, access_token,
                        expires_at, scopes, label=""):
    """Store or update a connected account.

    An empty refresh token does not overwrite a stored one: Google only sends
    it on the first consent, so a re-connect that omits it must not silently
    delete the ability to refresh.
    """
    import uuid
    enc_refresh = crypt.encrypt(refresh_token) if refresh_token else ""
    enc_access = crypt.encrypt(access_token) if access_token else ""
    existing = conn.execute("SELECT id, refresh_token FROM google_accounts"
                            " WHERE sub=?", (sub,)).fetchone()
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    if existing:
        conn.execute(
            "UPDATE google_accounts SET email=?, scopes=?, access_token=?,"
            " expires_at=?, refresh_token=COALESCE(NULLIF(?,''), refresh_token),"
            " last_error='' WHERE sub=?",
            (email, scopes, enc_access, expires_at, enc_refresh, sub))
        conn.commit()
        return existing["id"]
    aid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO google_accounts (id, sub, email, label, scopes,"
        " refresh_token, access_token, expires_at, connected_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (aid, sub, email, label or email, scopes, enc_refresh, enc_access,
         expires_at, now))
    conn.commit()
    return aid


def google_refresh_token(conn, account_id):
    row = conn.execute("SELECT refresh_token FROM google_accounts WHERE id=?",
                       (account_id,)).fetchone()
    return crypt.decrypt(row["refresh_token"]) if row else None


def disconnect_google(conn, account_id):
    """Forget an account entirely, tokens included."""
    n = conn.execute("DELETE FROM google_accounts WHERE id=?",
                     (account_id,)).rowcount
    conn.commit()
    return n


def note_sync(conn, account_id, error=""):
    conn.execute("UPDATE google_accounts SET last_sync_at=?, last_error=?"
                 " WHERE id=?",
                 (time.strftime("%Y-%m-%dT%H:%M:%S"), error[:300], account_id))
    conn.commit()


def json_default(o):
    return json.dumps(o, default=str)
