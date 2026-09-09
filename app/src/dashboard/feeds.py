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

from . import times


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
        # A tombstone for something we never had says nothing. Google keeps
        # cancelled events and re-serves them, so without this every deletion
        # comes back for ever as a hidden row. Storing it still matters when
        # we *do* have the event -- that is how a deletion made elsewhere
        # reaches the agenda instead of leaving a stale copy on it.
        if e.get("deleted") and row is None:
            continue
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
            1 if e.get("deleted") else 0,
            str(e.get("etag", ""))[:120],
            str(e.get("recurrence", ""))[:300],
            int(e.get("reminder_minutes", -1) or -1),
            str(e.get("series_id", ""))[:200],
            str(e.get("event_type", "default"))[:40])
        if row:
            conn.execute(
                "UPDATE events SET calendar_id=?, title=?, description=?,"
                " location=?, starts_at=?, ends_at=?, all_day=?, status=?,"
                " updated_at=?, deleted=?, etag=?, recurrence=?,"
                " reminder_minutes=?, series_id=?, event_type=? WHERE id=?",
                (*values[2:], row["id"]))
        else:
            conn.execute(
                "INSERT INTO events (id, account_id, source_uid, calendar_id,"
                " title, description, location, starts_at, ends_at, all_day,"
                " status, updated_at, deleted, etag, recurrence,"
                " reminder_minutes, series_id, event_type)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_id(), *values))
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
        " WHERE e.deleted=0 AND e.pending_delete=0 AND e.status <> 'cancelled'"
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
        # pending_delete too: something you have just deleted should leave the
        # grid at once, not linger until the next push confirms it.
        " WHERE e.deleted=0 AND e.pending_delete=0 AND e.status <> 'cancelled'"
        "   AND e.starts_at <= ?"
        "   AND COALESCE(NULLIF(e.ends_at,''), e.starts_at) >= ?"
        " ORDER BY e.starts_at LIMIT ?",
        (end, start, limit)).fetchall()
    return [dict(r) for r in rows]


# ── editing events ────────────────────────────────────────────────────────
EVENT_FIELDS = {"title", "description", "location", "starts_at", "ends_at",
                "all_day", "recurrence", "reminder_minutes"}

# What the form offers. Written out rather than accepting arbitrary RRULE from
# the browser: a rule is pushed straight to Google, and the set of things
# worth offering is small and knowable.
REPEATS = {
    "": "",
    "daily": "RRULE:FREQ=DAILY",
    "weekdays": "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
    "weekly": "RRULE:FREQ=WEEKLY",
    "fortnightly": "RRULE:FREQ=WEEKLY;INTERVAL=2",
    "monthly": "RRULE:FREQ=MONTHLY",
    "yearly": "RRULE:FREQ=YEARLY",
}

# Minutes before the start. -1 is the calendar's own default, which is a real
# choice and not an absent one.
REMINDERS = (-1, 0, 5, 10, 15, 30, 60, 120, 1440, 2880)


def _recurrence(value, stored=False):
    """A repeat rule.

    From a caller, only the named options are accepted: a rule goes straight
    into someone's real calendar, and the set worth offering is small enough
    to list. `stored` is for a rule already in the row -- Google's own rules
    are far richer than the list, and re-validating one on an unrelated edit
    would destroy it. The two paths are separate because letting the second
    serve the first is exactly how arbitrary input gets in.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text in REPEATS:
        return REPEATS[text]
    if stored and text.startswith("RRULE:") and len(text) < 300 \
            and "\n" not in text:
        return text
    raise ValueError("that repeat is not one of the options")


def _reminder(value):
    if value in (None, ""):
        return -1
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        raise ValueError("a reminder is a number of minutes") from None
    if minutes not in REMINDERS:
        raise ValueError("that reminder is not one of the options")
    return minutes


def _event_times(starts_at, ends_at, all_day):
    """Normalise a pair of times, or say why they cannot be used.

    Google treats an all-day end as exclusive -- a one-day event ends on the
    following midnight. Storing it inclusively would paint an extra cell on
    the grid and send Google a different day than the one shown.
    """
    start = times.to_utc(starts_at)
    if not times.parse(start):
        raise ValueError("a start time is required")
    end = times.to_utc(ends_at) if ends_at else ""
    if not end:
        end = times.plus(start, days=1) if all_day else times.plus(start, hours=1)
    if times.parse(end) < times.parse(start):
        raise ValueError("an event cannot end before it starts")
    if all_day:
        start = start[:10] + "T00:00:00"
        if end[:10] <= start[:10]:
            end = times.plus(start, days=1)
    return start, end


def create_event(conn, account_id, **fields):
    """A new event, local until a push gives it a provider id.

    The placeholder id is what lets several unsent events coexist: the table's
    uniqueness is on (account, source_uid), so they cannot all be ''.
    """
    bad = set(fields) - EVENT_FIELDS
    if bad:
        raise ValueError("cannot set: " + ", ".join(sorted(bad)))
    if conn.execute("SELECT 1 FROM google_accounts WHERE id=?",
                    (account_id,)).fetchone() is None:
        raise KeyError(account_id)

    all_day = 1 if fields.get("all_day") else 0
    start, end = _event_times(fields.get("starts_at"), fields.get("ends_at"),
                              all_day)
    eid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO events (id, account_id, source_uid, title, description,"
        " location, starts_at, ends_at, all_day, status, updated_at, dirty,"
        " recurrence, reminder_minutes)"
        " VALUES (?,?,?,?,?,?,?,?,?,'confirmed',?,1,?,?)",
        (eid, account_id, "local:" + uuid.uuid4().hex,
         str(fields.get("title") or "").strip()[:500],
         str(fields.get("description") or "")[:5000],
         str(fields.get("location") or "")[:500],
         start, end, all_day, _now(),
         _recurrence(fields.get("recurrence")),
         _reminder(fields.get("reminder_minutes"))))
    conn.commit()
    return eid


def update_event(conn, event_id, **fields):
    bad = set(fields) - EVENT_FIELDS
    if bad:
        raise ValueError("cannot set: " + ", ".join(sorted(bad)))
    row = conn.execute("SELECT * FROM events WHERE id=? AND deleted=0",
                       (event_id,)).fetchone()
    if row is None:
        raise KeyError(event_id)
    # Refused here rather than three minutes later by Google. A save that
    # appears to work and silently reverts is worse than one that says no.
    kind = row["event_type"] if "event_type" in row.keys() else "default"
    if kind and kind != "default":
        raise ValueError(
            "Google will not let an app change a %s event. Edit it in Google "
            "Calendar, or make your own event on that date." % kind)

    merged = {k: row[k] for k in EVENT_FIELDS}
    merged.update({k: v for k, v in fields.items() if v is not None})
    # Only a rule the caller actually sent is held to the offered list; one
    # already on the row is Google's and survives untouched.
    rule = _recurrence(merged.get("recurrence"),
                       stored="recurrence" not in fields)
    all_day = 1 if merged.get("all_day") else 0
    # Times are re-derived from the merged state, not the patch: changing only
    # all_day has to re-shape the times it did not mention.
    start, end = _event_times(merged.get("starts_at"), merged.get("ends_at"),
                              all_day)
    conn.execute(
        "UPDATE events SET title=?, description=?, location=?, starts_at=?,"
        " ends_at=?, all_day=?, updated_at=?, dirty=1, push_error='',"
        " push_attempts=0,"
        " recurrence=?, reminder_minutes=? WHERE id=?",
        (str(merged.get("title") or "").strip()[:500],
         str(merged.get("description") or "")[:5000],
         str(merged.get("location") or "")[:500],
         start, end, all_day, _now(), rule,
         _reminder(merged.get("reminder_minutes")), event_id))
    conn.commit()
    return event_id


def delete_event(conn, event_id):
    """Mark it for deletion. The row survives until Google has been told.

    Deleting the row outright would work locally and leave the event in the
    calendar for ever, which is the same silent divergence the push path
    exists to prevent.
    """
    row = conn.execute("SELECT id FROM events WHERE id=?", (event_id,)).fetchone()
    if row is None:
        raise KeyError(event_id)
    conn.execute("UPDATE events SET pending_delete=1, dirty=1, push_error='',"
                 " push_attempts=0 WHERE id=?", (event_id,))
    conn.commit()


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
            "SELECT id, dirty, importance_override, is_unread, read_at"
            " FROM messages WHERE account_id=? AND source_uid=?",
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
        # Mail is read on a phone as often as it is read here, and that
        # arrives as a label change on the next pull rather than as an edit.
        # Stamping only local edits would leave everything read elsewhere
        # sitting in the list for ever, which is most of it.
        unread_now = 1 if m.get("is_unread", True) else 0
        if row:
            read_at = row["read_at"] or ""
            # Stamped on the transition, and also whenever a read message is
            # found without one. The second case is not hypothetical: every
            # message already stored when the column was added is read with
            # no timestamp, and a rule that only fires on the transition
            # would exempt all of them for ever.
            if not unread_now and (row["is_unread"] or not read_at):
                read_at = _now()
            elif unread_now:
                read_at = ""
            conn.execute(
                "UPDATE messages SET thread_id=?, sender=?, sender_email=?,"
                " subject=?, snippet=?, received_at=?, is_unread=?,"
                " is_starred=?, archived=?, labels=?, importance=?, reason=?,"
                " model=?, classified_at=?, deleted=?, read_at=? WHERE id=?",
                (*common, read_at, row["id"]))
        else:
            # First seen already read: it was read before this existed, and
            # now is the only honest answer to when. Dated from arrival
            # instead, a fortnight of back-fill would retire on sight.
            conn.execute(
                "INSERT INTO messages (id, account_id, source_uid, thread_id,"
                " sender, sender_email, subject, snippet, received_at,"
                " is_unread, is_starred, archived, labels, importance, reason,"
                " model, classified_at, deleted, read_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_id(), account_id, uid, *common, "" if unread_now else _now()))
        written += 1
    conn.commit()
    return written, skipped


def _days_ago(n):
    import datetime as _dt
    return (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=n)).strftime(
        "%Y-%m-%dT%H:%M:%S")


def effective_importance_sql():
    """Your correction wins over the classifier's score."""
    return "COALESCE(m.importance_override, m.importance, 0)"


def inbox(conn, min_importance=3, limit=50, include_archived=False,
          read_days=0):
    """The triage list.

    `read_days` retires mail that has been read for longer than that. It is a
    list of what still wants you, and something dealt with a week ago does
    not -- left in place it only accumulates, and the unread mail the screen
    exists to surface gets harder to find every day.

    Nothing is deleted and nothing is unreachable: the caller can ask for
    everything, and the screen offers that.
    """
    where = ["m.deleted=0"]
    args_pre = []
    if not include_archived:
        where.append("m.archived=0")
    if read_days and int(read_days) > 0:
        # Unread mail is never retired however old, and neither is anything
        # starred: starring it is saying to keep it in front of you.
        where.append(
            "(m.is_unread=1 OR m.is_starred=1 OR m.read_at='' OR m.read_at > ?)")
        args_pre.append(_days_ago(int(read_days)))
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
        (*args_pre, min_importance, limit)).fetchall()
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
    # Stamped on the way from unread to read, and cleared going back, so a
    # triage list can retire what has been dealt with. Both directions
    # matter: marking something unread again is saying it still needs you.
    if "is_unread" in fields:
        now_read = not int(fields["is_unread"] or 0)
        was = conn.execute("SELECT is_unread FROM messages WHERE id=?",
                           (message_id,)).fetchone()
        if now_read and was and was["is_unread"]:
            fields["read_at"] = _now()
        elif not now_read:
            fields["read_at"] = ""

    values = [int(v) if isinstance(v, bool) else v for v in fields.values()]
    sets = ", ".join("%s=?" % k for k in fields)
    if fields.keys() & PUSHABLE:
        # A fresh edit gets a fresh budget of attempts: the last failure was
        # about the last edit, and holding it against this one would abandon
        # a change that has not been tried even once.
        sets += ", dirty=1, push_error='', push_attempts=0"
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
def unsent(conn, limit=20):
    """Edits that never reached the provider, with what each one said.

    An abandoned change is invisible otherwise: the screen shows what you
    asked for and the provider shows something else, and nothing connects the
    two. This is what lets the interface say so.
    """
    out = []
    for table, label in (("messages", "mail"), ("events", "calendar")):
        for r in conn.execute(
                "SELECT id, push_error FROM %s"
                " WHERE dirty=0 AND push_error<>'' LIMIT ?" % table, (limit,)):
            out.append({"kind": label, "id": r["id"], "why": r["push_error"]})
    return out


def sync_cursor(conn, source):
    """Where this feed got to last time, or None to start from the beginning."""
    row = conn.execute("SELECT cursor FROM sync_state WHERE source=?",
                       (source,)).fetchone()
    return (row["cursor"] or None) if row else None


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
