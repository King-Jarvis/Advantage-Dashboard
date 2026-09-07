"""Sending local edits back to Google.

Mostly tests about not losing things. A push that half-works, or that clears
its flag before Google agreed, loses an edit silently -- which is the exact
failure this module was written to end.
"""
import time

import pytest

from dashboard import crypt, feeds, google_api, push, settings


@pytest.fixture
def acct(conn, monkeypatch, tmp_path):
    k = tmp_path / "k"
    k.write_text("a-long-random-secret-for-these-tests")
    monkeypatch.setenv("TOKEN_KEY_PATH", str(k))
    crypt.reset_for_tests()
    aid = settings.save_google_account(conn, "sub-1", "a@example.com",
                                       "FAKE-REFRESH", "FAKE-ACCESS",
                                       time.time() + 3600, "s")
    yield aid
    crypt.reset_for_tests()


@pytest.fixture
def calls(monkeypatch):
    """Record every write instead of making one."""
    log = []

    def fake(conn, aid, url, method="POST", payload=None, etag=None):
        log.append({"url": url, "method": method, "payload": payload,
                    "etag": etag})
        if url.endswith("/events"):
            return {"id": "google-made-this", "etag": '"v1"'}
        return {"etag": '"v2"'}

    monkeypatch.setattr(google_api, "_send_retrying", fake)
    return log


def a_message(conn, acct, **kw):
    uid = kw.pop("uid", "m1")
    base = dict(source_uid=uid, received_at="2026-09-01T09:00:00",
                subject="Hello")
    base.update(kw)
    feeds.upsert_messages(conn, acct, [base])
    # Select by uid, not "the first row" -- without the WHERE this returned
    # whichever message was created first, so a second call silently handed
    # back the first message's id and both edits landed on one row.
    return conn.execute("SELECT id FROM messages WHERE source_uid=?",
                        (uid,)).fetchone()[0]


# ── label arithmetic ──────────────────────────────────────────────────────
def row(**kw):
    base = {"is_unread": 1, "is_starred": 0, "archived": 0, "is_spam": 0,
            "trashed": 0, "labels": ""}
    base.update(kw)
    return base


def test_archiving_removes_inbox():
    add, remove = push.label_delta(row(archived=1))
    assert "INBOX" in remove and "INBOX" not in add


def test_reading_removes_unread():
    add, remove = push.label_delta(row(is_unread=0))
    assert "UNREAD" in remove and "UNREAD" not in add


def test_starring_adds_starred():
    add, remove = push.label_delta(row(is_starred=1))
    assert "STARRED" in add and "STARRED" not in remove


def test_spam_leaves_the_inbox():
    add, remove = push.label_delta(row(is_spam=1))
    assert "SPAM" in add and "INBOX" in remove and "SPAM" not in remove


def test_no_label_is_both_added_and_removed():
    """A request that says both is undefined at best."""
    for r in (row(), row(archived=1, is_spam=1), row(is_starred=1, is_unread=0),
              row(is_spam=1, is_starred=1)):
        add, remove = push.label_delta(r)
        assert not (set(add) & set(remove)), (add, remove)


def test_the_delta_is_deterministic():
    """Same intent, same request -- which is what makes a retry provably
    identical to the first attempt."""
    assert push.label_delta(row(archived=1)) == push.label_delta(row(archived=1))


# ── the run ───────────────────────────────────────────────────────────────
def test_a_successful_push_clears_dirty(conn, acct, calls):
    mid = a_message(conn, acct)
    feeds.set_message(conn, mid, archived=1)
    assert conn.execute("SELECT dirty FROM messages").fetchone()[0] == 1

    out = push.run(conn)
    assert out["ok"] and out["results"][0]["pushed"] == 1
    assert conn.execute("SELECT dirty FROM messages").fetchone()[0] == 0
    # The full desired state, not just the change. Asserting every label
    # every time is what makes a repeated push provably a no-op.
    assert calls[0]["payload"]["removeLabelIds"] == ["INBOX", "SPAM", "STARRED"]
    assert calls[0]["payload"]["addLabelIds"] == ["UNREAD"]


def test_a_failed_push_keeps_the_edit(conn, acct, monkeypatch):
    """The whole point. If a failure cleared the flag, the next poll would
    overwrite the edit and it would be gone with no trace."""
    def boom(*a, **k):
        raise google_api.GoogleError("Gmail said no")
    monkeypatch.setattr(google_api, "_send_retrying", boom)

    mid = a_message(conn, acct)
    feeds.set_message(conn, mid, archived=1)
    out = push.run(conn)

    assert out["ok"] is False and out["results"][0]["failed"] == 1
    r = conn.execute("SELECT dirty, push_error FROM messages").fetchone()
    assert r["dirty"] == 1, "a failed push cleared the flag"
    assert "Gmail said no" in r["push_error"]


def test_one_bad_row_does_not_strand_the_others(conn, acct, monkeypatch):
    good = a_message(conn, acct, uid="ok")
    bad = a_message(conn, acct, uid="bad")
    feeds.set_message(conn, good, archived=1)
    feeds.set_message(conn, bad, archived=1)

    def selective(conn_, aid, url, method="POST", payload=None, etag=None):
        if "bad" in url:
            raise google_api.GoogleError("nope")
        return {}
    monkeypatch.setattr(google_api, "_send_retrying", selective)

    push.run(conn)
    states = dict(conn.execute("SELECT source_uid, dirty FROM messages"))
    assert states == {"ok": 0, "bad": 1}


def test_pushing_twice_is_harmless(conn, acct, calls):
    mid = a_message(conn, acct)
    feeds.set_message(conn, mid, is_starred=1)
    push.run(conn)
    first = len(calls)
    # Nothing is dirty now, so a second run must do nothing at all.
    push.run(conn)
    assert len(calls) == first


def test_an_importance_correction_is_never_pushed(conn, acct, calls):
    """Google has no idea what importance means. Sending one would be a
    request with nothing in it."""
    mid = a_message(conn, acct)
    feeds.set_message(conn, mid, importance_override=5)
    push.run(conn)
    assert calls == []


def test_trashing_uses_trash_not_a_label(conn, acct, calls):
    mid = a_message(conn, acct)
    feeds.set_message(conn, mid, trashed=1)
    push.run(conn)
    assert calls[0]["url"].endswith("/trash")


# ── calendar ──────────────────────────────────────────────────────────────
def an_event(conn, acct, uid="e1", **kw):
    base = dict(source_uid=uid, starts_at="2026-09-10T09:00:00",
                ends_at="2026-09-10T10:00:00", title="Standup")
    base.update(kw)
    feeds.upsert_events(conn, acct, [base])
    return conn.execute("SELECT id FROM events WHERE source_uid=?",
                        (uid,)).fetchone()[0]


def test_a_local_event_is_inserted_and_gets_googles_id(conn, acct, calls):
    eid = an_event(conn, acct, uid="local:abc123")
    conn.execute("UPDATE events SET dirty=1 WHERE id=?", (eid,))
    push.run(conn)
    assert calls[0]["method"] == "POST" and calls[0]["url"].endswith("/events")
    assert conn.execute("SELECT source_uid FROM events WHERE id=?",
                        (eid,)).fetchone()[0] == "google-made-this"


def test_an_existing_event_is_patched_with_its_etag(conn, acct, calls):
    eid = an_event(conn, acct)
    conn.execute("UPDATE events SET dirty=1, etag='\"v1\"' WHERE id=?", (eid,))
    push.run(conn)
    assert calls[0]["method"] == "PATCH" and calls[0]["etag"] == '"v1"'


def test_a_conflict_does_not_clobber_google(conn, acct, monkeypatch):
    """412 means their copy moved. Ours waits and says so rather than winning
    by accident."""
    def conflict(*a, **k):
        raise google_api.Conflict()
    monkeypatch.setattr(google_api, "_send_retrying", conflict)

    eid = an_event(conn, acct)
    conn.execute("UPDATE events SET dirty=1 WHERE id=?", (eid,))
    out = push.run(conn)

    assert out["results"][0]["conflicts"] == 1
    r = conn.execute("SELECT dirty, push_error FROM events").fetchone()
    assert r["dirty"] == 1
    assert "changed in Google" in r["push_error"]


def test_deleting_a_never_synced_event_needs_no_request(conn, acct, calls):
    eid = an_event(conn, acct, uid="local:xyz")
    conn.execute("UPDATE events SET dirty=1, pending_delete=1 WHERE id=?", (eid,))
    push.run(conn)
    assert calls == [], "asked Google to delete something it never had"
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_deleting_a_synced_event_calls_delete_then_forgets_it(conn, acct, calls):
    eid = an_event(conn, acct)
    conn.execute("UPDATE events SET dirty=1, pending_delete=1 WHERE id=?", (eid,))
    push.run(conn)
    assert calls[0]["method"] == "DELETE"
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_an_all_day_event_sends_date_not_datetime(conn, acct, calls):
    eid = an_event(conn, acct, uid="local:allday", all_day=1,
                   ends_at="2026-09-11T00:00:00")
    conn.execute("UPDATE events SET dirty=1 WHERE id=?", (eid,))
    push.run(conn)
    start = calls[0]["payload"]["start"]
    assert "date" in start and "dateTime" not in start
    assert start["date"] == "2026-09-10"


def test_pending_count_reports_what_is_waiting(conn, acct):
    mid = a_message(conn, acct)
    assert push.pending_count(conn) == 0
    feeds.set_message(conn, mid, archived=1)
    assert push.pending_count(conn) == 1


def test_a_timed_event_carries_an_offset(conn, acct, calls):
    """Google rejects a dateTime with neither offset nor timeZone, answering
    400 'required' without naming the field. Storage is naive UTC, so Z."""
    eid = an_event(conn, acct, uid="local:timed")
    conn.execute("UPDATE events SET dirty=1 WHERE id=?", (eid,))
    push.run(conn)
    start = calls[0]["payload"]["start"]["dateTime"]
    end = calls[0]["payload"]["end"]["dateTime"]
    assert start.endswith("Z") and end.endswith("Z"), (start, end)


def test_an_offset_is_not_doubled(conn, acct, calls):
    eid = an_event(conn, acct, uid="local:already",
                   starts_at="2026-09-10T09:00:00Z")
    conn.execute("UPDATE events SET dirty=1 WHERE id=?", (eid,))
    push.run(conn)
    assert calls[0]["payload"]["start"]["dateTime"].count("Z") == 1


def test_all_day_events_get_no_offset(conn, acct, calls):
    eid = an_event(conn, acct, uid="local:day", all_day=1)
    conn.execute("UPDATE events SET dirty=1 WHERE id=?", (eid,))
    push.run(conn)
    assert calls[0]["payload"]["start"] == {"date": "2026-09-10"}


def test_a_repeat_is_sent_as_a_list(conn, acct, calls):
    """Google's shape: a recurrence can carry EXDATE and RDATE alongside the
    rule, so it is always a list."""
    feeds.create_event(conn, acct, title="Standup",
                       starts_at="2026-09-15T09:00:00", recurrence="weekly")
    push.run(conn)
    assert calls[0]["payload"]["recurrence"] == ["RRULE:FREQ=WEEKLY"]


def test_a_repeating_event_is_not_left_behind_as_a_phantom(conn, acct, calls):
    """Google keeps one series; we sync with singleEvents and get instances
    with their own ids. Keeping the row we inserted would leave a duplicate
    sitting at the series start."""
    feeds.create_event(conn, acct, title="Standup",
                       starts_at="2026-09-15T09:00:00", recurrence="weekly")
    push.run(conn)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_a_one_off_event_keeps_its_row(conn, acct, calls):
    eid = feeds.create_event(conn, acct, title="Dentist",
                             starts_at="2026-09-15T09:00:00")
    push.run(conn)
    r = conn.execute("SELECT source_uid, dirty FROM events WHERE id=?",
                     (eid,)).fetchone()
    assert r["source_uid"] == "google-made-this" and r["dirty"] == 0


def test_a_reminder_becomes_an_override(conn, acct, calls):
    feeds.create_event(conn, acct, title="x", starts_at="2026-09-15T09:00:00",
                       reminder_minutes=30)
    push.run(conn)
    rem = calls[0]["payload"]["reminders"]
    assert rem["useDefault"] is False
    assert rem["overrides"] == [{"method": "popup", "minutes": 30}]


def test_the_default_reminder_is_sent_explicitly(conn, acct, calls):
    """On a patch, sending nothing would leave a previous override in place."""
    feeds.create_event(conn, acct, title="x", starts_at="2026-09-15T09:00:00")
    push.run(conn)
    assert calls[0]["payload"]["reminders"] == {"useDefault": True}


def test_a_timed_event_names_its_timezone(conn, acct, calls):
    """A Z offset satisfies a one-off. A recurring event does not go without
    an explicit timeZone -- Google answers 400 'required' and names nothing."""
    feeds.create_event(conn, acct, title="Standup",
                       starts_at="2026-09-15T09:00:00", recurrence="weekly")
    push.run(conn)
    start = calls[0]["payload"]["start"]
    assert start["timeZone"] == "UTC"
    assert calls[0]["payload"]["end"]["timeZone"] == "UTC"


def test_an_all_day_event_names_no_timezone(conn, acct, calls):
    """A date has no time of day to place in a zone."""
    feeds.create_event(conn, acct, title="Holiday", all_day=True,
                       starts_at="2026-09-15T00:00:00")
    push.run(conn)
    assert "timeZone" not in calls[0]["payload"]["start"]


def test_a_permanent_refusal_stops_retrying(conn, acct, monkeypatch):
    """Retrying for ever keeps a queue that never drains, and leaving the
    local edit in place means the two copies disagree for good. Clearing the
    flag lets the next pull restore what Google actually holds."""
    def refused(*a, **k):
        raise google_api.Refused("Google will not change a birthday")
    monkeypatch.setattr(google_api, "_send_retrying", refused)

    eid = an_event(conn, acct)
    conn.execute("UPDATE events SET dirty=1 WHERE id=?", (eid,))
    out = push.run(conn)

    r = conn.execute("SELECT dirty, push_error FROM events").fetchone()
    assert r["dirty"] == 0, "a permanent refusal stayed queued"
    assert "birthday" in r["push_error"]
    assert out["results"][0]["failed"] == 1


def test_a_transient_failure_still_retries(conn, acct, monkeypatch):
    """The distinction is the point: only the permanent ones give up."""
    def boom(*a, **k):
        raise google_api.GoogleError("network wobble")
    monkeypatch.setattr(google_api, "_send_retrying", boom)

    eid = an_event(conn, acct)
    conn.execute("UPDATE events SET dirty=1 WHERE id=?", (eid,))
    push.run(conn)
    assert conn.execute("SELECT dirty FROM events").fetchone()[0] == 1
