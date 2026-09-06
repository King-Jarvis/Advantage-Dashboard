"""Sending local edits back to Google.

The other half of sync.py, and the half that was missing. Until this existed,
`dirty` protected a local edit from being overwritten by the next poll and
nothing ever sent it anywhere -- so archiving a message hid it on Eva and left
it sitting in Gmail, for ever, with no sign that the two had parted ways.

Two rules shape everything here.

A row stays dirty until Google has confirmed the change. Clearing the flag
first would mean a failed push looks exactly like a successful one, and the
edit is then silently lost at the next poll, which is the specific failure
this module exists to prevent.

And every operation is expressed so that repeating it is harmless. Label
arithmetic is a set operation, trash is a state rather than a transition, and
a delete of something already gone is a success. Retries are therefore safe
without any bookkeeping about what was already attempted.
"""
import time

from . import feeds, google_api, settings

GMAIL_MODIFY = "https://gmail.googleapis.com/gmail/v1/users/me/messages/%s/modify"
GMAIL_TRASH = "https://gmail.googleapis.com/gmail/v1/users/me/messages/%s/trash"
GMAIL_UNTRASH = "https://gmail.googleapis.com/gmail/v1/users/me/messages/%s/untrash"
CAL_EVENTS = "https://www.googleapis.com/calendar/v3/calendars/primary/events"

LOCAL_PREFIX = "local:"


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ── mail ──────────────────────────────────────────────────────────────────
def label_delta(row):
    """The labels to add and remove to make Gmail match this row.

    Returned as sorted lists so the same intent always produces the same
    request, which is what makes this testable and what makes a retry
    provably identical to the first attempt.
    """
    add, remove = set(), set()
    (remove if not row["is_unread"] else add).add("UNREAD")
    (add if row["is_starred"] else remove).add("STARRED")
    # Archiving is the removal of INBOX; there is no ARCHIVED label.
    (remove if row["archived"] else add).add("INBOX")
    if row["is_spam"]:
        add.add("SPAM")
        remove.add("INBOX")
        remove.discard("SPAM")
    else:
        remove.add("SPAM")
    return sorted(add - remove), sorted(remove)


def push_message(conn, account_id, row):
    uid = row["source_uid"]
    if row["trashed"]:
        # Trash first and skip the label work: Gmail moves the message out of
        # every label anyway, so a modify alongside it would be noise.
        google_api._send_retrying(conn, account_id, GMAIL_TRASH % uid)
        return
    if "TRASH" in (row["labels"] or ""):
        # It is in the bin at Google and not in the bin here, so the edit
        # being pushed is the un-trashing. modify cannot do this: TRASH is not
        # an ordinary label and only untrash removes it.
        google_api._send_retrying(conn, account_id, GMAIL_UNTRASH % uid)

    add, remove = label_delta(row)
    google_api._send_retrying(
        conn, account_id, GMAIL_MODIFY % uid,
        payload={"addLabelIds": add, "removeLabelIds": remove})


# ── calendar ──────────────────────────────────────────────────────────────
def event_payload(row):
    """A row as Google's Events resource.

    All-day events use `date`; timed ones use `dateTime`. Google treats an
    all-day end as exclusive, which is the convention already stored, so it
    passes through untouched.
    """
    key = "date" if row["all_day"] else "dateTime"
    def stamp(value):
        text = str(value or "")
        return text[:10] if row["all_day"] else text
    body = {
        "summary": row["title"] or "",
        "start": {key: stamp(row["starts_at"])},
        "end": {key: stamp(row["ends_at"] or row["starts_at"])},
    }
    if row["location"]:
        body["location"] = row["location"]
    if row["description"]:
        body["description"] = row["description"]
    return body


def push_event(conn, account_id, row):
    uid = row["source_uid"] or ""
    local = uid.startswith(LOCAL_PREFIX) or not uid

    if row["pending_delete"]:
        if local:
            # Created here and deleted here: Google never heard of it, so
            # there is nothing to withdraw.
            conn.execute("DELETE FROM events WHERE id=?", (row["id"],))
            return
        try:
            google_api._send_retrying(conn, account_id,
                                      "%s/%s" % (CAL_EVENTS, uid),
                                      method="DELETE")
        except google_api.GoogleError as e:
            # Already gone is the outcome we wanted.
            if "410" not in str(e) and "404" not in str(e):
                raise
        conn.execute("DELETE FROM events WHERE id=?", (row["id"],))
        return

    if local:
        got = google_api._send_retrying(conn, account_id, CAL_EVENTS,
                                        payload=event_payload(row))
        new_uid = str(got.get("id") or "")
        if not new_uid:
            raise google_api.GoogleError("Google accepted the event but "
                                         "returned no id")
        conn.execute("UPDATE events SET source_uid=?, etag=? WHERE id=?",
                     (new_uid, str(got.get("etag") or ""), row["id"]))
        return

    got = google_api._send_retrying(
        conn, account_id, "%s/%s" % (CAL_EVENTS, uid), method="PATCH",
        payload=event_payload(row), etag=row["etag"] or None)
    conn.execute("UPDATE events SET etag=? WHERE id=?",
                 (str(got.get("etag") or ""), row["id"]))


# ── the run ───────────────────────────────────────────────────────────────
def _pending(conn, table, account_id):
    return conn.execute(
        "SELECT * FROM %s WHERE account_id=? AND dirty=1"
        " ORDER BY rowid" % table, (account_id,)).fetchall()


def run(conn, account_id=None):
    """Push every pending edit. Returns a per-account summary.

    One row failing must not stop the rest: a single message Gmail refuses
    should not strand a week of other edits behind it.
    """
    accounts = settings.list_google_accounts(conn)
    if account_id:
        accounts = [a for a in accounts if a["id"] == account_id]

    results = []
    for account in accounts:
        aid = account["id"]
        pushed = failed = conflicts = 0
        for table, fn in (("messages", push_message), ("events", push_event)):
            for row in _pending(conn, table, aid):
                try:
                    fn(conn, aid, row)
                except google_api.Conflict:
                    conflicts += 1
                    conn.execute(
                        "UPDATE %s SET push_error=? WHERE id=?" % table,
                        ("changed in Google since you edited it here; "
                         "your version has not been sent", row["id"]))
                except Exception as e:
                    failed += 1
                    conn.execute(
                        "UPDATE %s SET push_error=? WHERE id=?" % table,
                        (str(e)[:300], row["id"]))
                else:
                    pushed += 1
                    # Only now: the edit has actually landed.
                    conn.execute(
                        "UPDATE %s SET dirty=0, push_error='' WHERE id=?"
                        % table, (row["id"],))
                conn.commit()
        results.append({"account": account.get("email", ""), "pushed": pushed,
                        "failed": failed, "conflicts": conflicts})

    total_bad = sum(r["failed"] + r["conflicts"] for r in results)
    feeds.note_sync(conn, "push", "ok" if total_bad == 0 else "partial",
                    "" if total_bad == 0 else "%d edits not sent" % total_bad)
    return {"ok": total_bad == 0, "results": results}


def pending_count(conn):
    """How many edits are waiting. Shown so a stuck queue is visible."""
    return sum(conn.execute(
        "SELECT COUNT(*) FROM %s WHERE dirty=1" % t).fetchone()[0]
        for t in ("messages", "events"))
