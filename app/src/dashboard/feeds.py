"""Calendar events and mail, as they arrive and as they are read back.

The provider owns this data; what is here is a cache. One exception: a row
marked `dirty` carries a local edit that has not reached the provider yet,
and a poll must not overwrite it. Without that rule an edit made seconds
before a sync silently disappears, which is the kind of bug people stop
trusting an application over.

Ingest is idempotent on (account, provider id), so replaying a sync repairs
rather than duplicates.
"""

import json
import time
import uuid


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _id():
    return uuid.uuid4().hex


# ── calendar ──────────────────────────────────────────────────────────────
def upsert_events(conn, account_id, events):
    """Write a batch of events. Returns (written, skipped_dirty)."""
    written = skipped = 0
    for e in events:
        uid = str(e.get("source_uid") or "").strip()
        starts = str(e.get("starts_at") or "").strip()
        if not uid or not starts:
            continue
        row = conn.execute(
            "SELECT id, dirty FROM events WHERE account_id=? AND source_uid=?",
            (account_id, uid)).fetchone()
        if row and row["dirty"]:
            # A local edit is waiting to be pushed. The provider's copy is
            # older than what the operator asked for.
            skipped += 1
            continue
        values = (
            account_id, uid, str(e.get("calendar_id", ""))[:200],
            str(e.get("title", ""))[:500], str(e.get("description", ""))[:2000],
            str(e.get("location", ""))[:500], starts,
            e.get("ends_at"), 1 if e.get("all_day") else 0,
            str(e.get("status", "confirmed"))[:40], _now(),
            1 if e.get("deleted") else 0)
        if row:
            conn.execute(
                "UPDATE events SET calendar_id=?, title=?, description=?,"
                " location=?, starts_at=?, ends_at=?, all_day=?, status=?,"
                " updated_at=?, deleted=? WHERE id=?",
                (*values[2:], row["id"]))
        else:
            conn.execute(
                "INSERT INTO events (id, account_id, source_uid, calendar_id,"
                " title, description, location, starts_at, ends_at, all_day,"
                " status, updated_at, deleted)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (_id(), *values))
        written += 1
    conn.commit()
    return written, skipped


def agenda(conn, days=7, limit=100, now=None):
    """Upcoming events, soonest first.

    Anything that has already finished is left out: an agenda is about what is
    ahead, and yesterday's meetings pushing today's off the top is how a
    widget stops being glanced at.
    """
    now = now or _now()
    horizon = time.strftime(
        "%Y-%m-%dT23:59:59",
        time.localtime(time.mktime(time.strptime(now[:10], "%Y-%m-%d"))
                       + days * 86400))
    rows = conn.execute(
        "SELECT e.*, g.email account_email FROM events e"
        " JOIN google_accounts g ON g.id = e.account_id"
        " WHERE e.deleted=0 AND e.status <> 'cancelled'"
        "   AND COALESCE(e.ends_at, e.starts_at) >= ?"
        "   AND e.starts_at <= ?"
        " ORDER BY e.starts_at LIMIT ?", (now, horizon, limit)).fetchall()
    return [dict(r) for r in rows]


def events_between(conn, start, end, limit=500):
    """Every event touching a date range, past ones included.

    Deliberately not agenda(): a calendar grid must show the days that have
    already happened, because a month with the first fortnight blank is not a
    month. An event is included when it overlaps the range at all, so a
    multi-day event appears in every month it touches rather than only the one
    it started in.
    """
    rows = conn.execute(
        "SELECT e.*, g.email account_email FROM events e"
        " JOIN google_accounts g ON g.id = e.account_id"
        " WHERE e.deleted=0 AND e.status <> 'cancelled'"
        "   AND e.starts_at <= ?"
        "   AND COALESCE(NULLIF(e.ends_at,''), e.starts_at) >= ?"
        " ORDER BY e.starts_at LIMIT ?",
        (end, start, limit)).fetchall()
    return [dict(r) for r in rows]


# ── mail ──────────────────────────────────────────────────────────────────
def upsert_messages(conn, account_id, messages):
    """Write a batch of messages. Returns (written, skipped_dirty).

    An importance the operator has overridden is never replaced by a fresh
    classification -- a correction that has to be made twice is not a
    correction.
    """
    written = skipped = 0
    for m in messages:
        uid = str(m.get("source_uid") or "").strip()
        received = str(m.get("received_at") or "").strip()
        if not uid or not received:
            continue
        row = conn.execute(
            "SELECT id, dirty, importance_override FROM messages"
            " WHERE account_id=? AND source_uid=?",
            (account_id, uid)).fetchone()
        if row and row["dirty"]:
            skipped += 1
            continue

        importance = m.get("importance")
        if importance is not None:
            try:
                importance = max(1, min(5, int(importance)))
            except (TypeError, ValueError):
                importance = None

        common = (
            str(m.get("thread_id", ""))[:200], str(m.get("sender", ""))[:300],
            str(m.get("sender_email", ""))[:300],
            str(m.get("subject", ""))[:500], str(m.get("snippet", ""))[:1000],
            received, 1 if m.get("is_unread", True) else 0,
            1 if m.get("is_starred") else 0,
            1 if m.get("archived") else 0, str(m.get("labels", ""))[:400],
            importance, str(m.get("reason", ""))[:400],
            str(m.get("model", ""))[:80], m.get("classified_at") or _now(),
            1 if m.get("deleted") else 0)
        if row:
            conn.execute(
                "UPDATE messages SET thread_id=?, sender=?, sender_email=?,"
                " subject=?, snippet=?, received_at=?, is_unread=?,"
                " is_starred=?, archived=?, labels=?, importance=?, reason=?,"
                " model=?, classified_at=?, deleted=? WHERE id=?",
                (*common, row["id"]))
        else:
            conn.execute(
                "INSERT INTO messages (id, account_id, source_uid, thread_id,"
                " sender, sender_email, subject, snippet, received_at,"
                " is_unread, is_starred, archived, labels, importance, reason,"
                " model, classified_at, deleted)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_id(), account_id, uid, *common))
        written += 1
    conn.commit()
    return written, skipped


def effective_importance_sql():
    """Your correction wins over the classifier's score."""
    return "COALESCE(m.importance_override, m.importance, 0)"


def inbox(conn, min_importance=3, limit=50, include_archived=False):
    where = ["m.deleted=0"]
    if not include_archived:
        where.append("m.archived=0")
    # Every column except the body. m.* would ship up to 256 KB per message
    # once bodies are cached, turning a list of forty headers into megabytes
    # -- and the list never shows a body. It is fetched per message instead.
    rows = conn.execute(
        "SELECT m.id, m.account_id, m.source_uid, m.thread_id, m.sender,"
        " m.sender_email, m.subject, m.snippet, m.received_at, m.is_unread,"
        " m.is_starred, m.archived, m.trashed, m.is_spam, m.labels,"
        " m.importance, m.importance_override, m.reason, m.model,"
        " m.classified_at, m.dirty, m.push_error,"
        " m.body_text IS NOT NULL AS has_body,"
        " %s AS score, g.email account_email FROM messages m"
        " JOIN google_accounts g ON g.id = m.account_id"
        " WHERE %s AND %s >= ?"
        " ORDER BY score DESC, m.received_at DESC LIMIT ?"
        % (effective_importance_sql(), " AND ".join(where),
           effective_importance_sql()),
        (min_importance, limit)).fetchall()
    return [dict(r) for r in rows]


# Fields Google knows about. Changing one of these means the provider's copy
# is now behind, so the row is dirty until a push says otherwise.
PUSHABLE = {"archived", "is_unread", "is_starred", "trashed", "is_spam"}
# Fields that exist only here. An importance correction is ours alone; marking
# it dirty would block the row from receiving fresh label state from Gmail
# until a pointless round trip cleared the flag.
LOCAL_ONLY = {"importance_override"}


def set_message(conn, message_id, **fields):
    """Apply a local change, and mark it as not yet pushed."""
    allowed = PUSHABLE | LOCAL_ONLY
    bad = set(fields) - allowed
    if bad:
        raise ValueError("cannot set: " + ", ".join(sorted(bad)))
    row = conn.execute("SELECT id FROM messages WHERE id=? AND deleted=0",
                       (message_id,)).fetchone()
    if row is None:
        raise KeyError(message_id)
    if "importance_override" in fields and fields["importance_override"] is not None:
        v = int(fields["importance_override"])
        if not 1 <= v <= 5:
            raise ValueError("importance must be between 1 and 5")
        fields["importance_override"] = v
    values = [int(v) if isinstance(v, bool) else v for v in fields.values()]
    sets = ", ".join("%s=?" % k for k in fields)
    if fields.keys() & PUSHABLE:
        sets += ", dirty=1, push_error=''"
    conn.execute("UPDATE messages SET %s WHERE id=?" % sets,
                 [*values, message_id])
    conn.commit()


def message_body(conn, message_id, fetch=None):
    """The body as (text, blocks, cached), fetched once and remembered.

    `fetch` is injected so the caller supplies the network, which keeps this
    module free of Google and keeps the tests free of the network.
    """
    row = conn.execute(
        "SELECT id, account_id, source_uid, body_text, body_blocks"
        " FROM messages WHERE id=? AND deleted=0", (message_id,)).fetchone()
    if row is None:
        raise KeyError(message_id)
    if row["body_text"] is not None:
        return row["body_text"], _blocks(row["body_blocks"]), True
    if fetch is None:
        return "", [], False

    got = fetch(row["account_id"], row["source_uid"])
    # Accept either form so an older caller that returns just text still
    # works rather than storing the string one character per block.
    text, blocks = got if isinstance(got, tuple) else (got or "", [])
    conn.execute(
        "UPDATE messages SET body_text=?, body_blocks=?, body_fetched_at=?"
        " WHERE id=?",
        (text or "", json.dumps(blocks, separators=(",", ":")), _now(),
         message_id))
    conn.commit()
    return text or "", blocks, False


def _blocks(raw):
    if not raw:
        return []
    try:
        got = json.loads(raw)
        return got if isinstance(got, list) else []
    except (TypeError, ValueError):
        return []


# ── sync bookkeeping ──────────────────────────────────────────────────────
def note_sync(conn, source, status="ok", error="", cursor=None):
    now = _now()
    conn.execute(
        "INSERT INTO sync_state (source, cursor, last_run_at, last_ok_at,"
        " last_status, last_error) VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(source) DO UPDATE SET"
        "   cursor=COALESCE(?, sync_state.cursor), last_run_at=?,"
        "   last_ok_at=CASE WHEN ?='ok' THEN ? ELSE sync_state.last_ok_at END,"
        "   last_status=?, last_error=?",
        (source, cursor or "", now, now if status == "ok" else None,
         status, error[:300], cursor, now, status, now, status, error[:300]))
    conn.commit()


def sync_status(conn):
    rows = conn.execute("SELECT * FROM sync_state ORDER BY source").fetchall()
    return [dict(r) for r in rows]
