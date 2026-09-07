"""Calendar and mail: ingest, local edits, and what a poll must not undo."""

import pytest

from dashboard import feeds, settings


@pytest.fixture
def acct(conn, monkeypatch, tmp_path):
    from dashboard import crypt
    k = tmp_path / "k"
    k.write_text("a-long-random-secret-for-these-tests")
    monkeypatch.setenv("TOKEN_KEY_PATH", str(k))
    crypt.reset_for_tests()
    aid = settings.save_google_account(conn, "sub-1", "a@example.com",
                                       "FAKE-REFRESH", "FAKE-ACCESS", None, "s")
    yield aid
    crypt.reset_for_tests()


def ev(uid, starts, **kw):
    return dict(source_uid=uid, starts_at=starts, title=kw.pop("title", uid), **kw)


def msg(uid, when, **kw):
    return dict(source_uid=uid, received_at=when,
                subject=kw.pop("subject", uid), **kw)


# ── calendar ──────────────────────────────────────────────────────────────
def test_events_are_written_and_read_back_in_order(conn, acct):
    feeds.upsert_events(conn, acct, [
        ev("b", "2026-09-10T14:00:00", title="Later"),
        ev("a", "2026-09-10T09:00:00", title="Earlier"),
    ])
    got = feeds.agenda(conn, now="2026-09-10T08:00:00")
    assert [e["title"] for e in got] == ["Earlier", "Later"]


def test_ingest_is_idempotent(conn, acct):
    for _ in range(3):
        feeds.upsert_events(conn, acct, [ev("a", "2026-09-10T09:00:00")])
    assert conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 1


def test_an_event_is_updated_in_place(conn, acct):
    feeds.upsert_events(conn, acct, [ev("a", "2026-09-10T09:00:00", title="Old")])
    feeds.upsert_events(conn, acct, [ev("a", "2026-09-10T10:00:00", title="New")])
    got = feeds.agenda(conn, now="2026-09-10T08:00:00")
    assert len(got) == 1 and got[0]["title"] == "New"


def test_finished_events_drop_off_the_agenda(conn, acct):
    """Yesterday's meetings pushing today's off the top is how a widget stops
    being looked at."""
    feeds.upsert_events(conn, acct, [
        ev("past", "2026-09-09T09:00:00", ends_at="2026-09-09T10:00:00"),
        ev("now", "2026-09-10T15:00:00", ends_at="2026-09-10T16:00:00"),
    ])
    got = feeds.agenda(conn, now="2026-09-10T12:00:00")
    assert [e["source_uid"] for e in got] == ["now"]


def test_an_event_in_progress_is_still_shown(conn, acct):
    feeds.upsert_events(conn, acct, [
        ev("running", "2026-09-10T11:00:00", ends_at="2026-09-10T13:00:00")])
    got = feeds.agenda(conn, now="2026-09-10T12:00:00")
    assert len(got) == 1


def test_cancelled_and_deleted_events_are_hidden(conn, acct):
    feeds.upsert_events(conn, acct, [
        ev("x", "2026-09-10T15:00:00", status="cancelled"),
        ev("y", "2026-09-10T16:00:00", deleted=True),
        ev("z", "2026-09-10T17:00:00"),
    ])
    got = feeds.agenda(conn, now="2026-09-10T08:00:00")
    assert [e["source_uid"] for e in got] == ["z"]


def test_the_horizon_is_respected(conn, acct):
    feeds.upsert_events(conn, acct, [
        ev("soon", "2026-09-11T09:00:00"),
        ev("far", "2026-10-20T09:00:00"),
    ])
    got = feeds.agenda(conn, days=7, now="2026-09-10T08:00:00")
    assert [e["source_uid"] for e in got] == ["soon"]


def test_rows_without_an_id_or_a_time_are_ignored(conn, acct):
    written, _ = feeds.upsert_events(conn, acct, [
        {"title": "no id"}, {"source_uid": "x"}, ev("ok", "2026-09-10T09:00:00")])
    assert written == 1


# ── mail ──────────────────────────────────────────────────────────────────
def test_messages_rank_by_importance_then_recency(conn, acct):
    feeds.upsert_messages(conn, acct, [
        msg("a", "2026-09-10T09:00:00", importance=3),
        msg("b", "2026-09-10T10:00:00", importance=5),
        msg("c", "2026-09-10T11:00:00", importance=3),
    ])
    got = feeds.inbox(conn, min_importance=1)
    assert [m["source_uid"] for m in got] == ["b", "c", "a"]


def test_the_threshold_filters(conn, acct):
    feeds.upsert_messages(conn, acct, [
        msg("low", "2026-09-10T09:00:00", importance=1),
        msg("high", "2026-09-10T09:00:00", importance=5),
    ])
    assert [m["source_uid"] for m in feeds.inbox(conn, min_importance=3)] == ["high"]


def test_importance_is_clamped_to_the_scale(conn, acct):
    feeds.upsert_messages(conn, acct, [
        msg("a", "2026-09-10T09:00:00", importance=99),
        msg("b", "2026-09-10T09:00:00", importance=-4),
        msg("c", "2026-09-10T09:00:00", importance="nonsense"),
    ])
    rows = {m["source_uid"]: m["importance"] for m in feeds.inbox(conn, 0)}
    # A model returning 99 must not outrank everything forever.
    assert rows["a"] == 5 and rows["b"] == 1 and rows["c"] is None


def test_archived_messages_are_hidden_by_default(conn, acct):
    feeds.upsert_messages(conn, acct, [msg("a", "2026-09-10T09:00:00",
                                           importance=5, archived=True)])
    assert feeds.inbox(conn, 1) == []
    assert len(feeds.inbox(conn, 1, include_archived=True)) == 1


# ── local edits, and what a poll must not undo ────────────────────────────
def test_a_local_edit_survives_the_next_poll(conn, acct):
    """The bug this prevents is silent: you archive something, a sync lands a
    second later, and it comes back with no explanation."""
    feeds.upsert_messages(conn, acct, [msg("a", "2026-09-10T09:00:00",
                                           importance=5)])
    row = feeds.inbox(conn, 1)[0]
    feeds.set_message(conn, row["id"], archived=True)

    written, skipped = feeds.upsert_messages(
        conn, acct, [msg("a", "2026-09-10T09:00:00", importance=5,
                         archived=False)])
    assert (written, skipped) == (0, 1)
    assert feeds.inbox(conn, 1) == []


def test_a_correction_outranks_the_classifier(conn, acct):
    feeds.upsert_messages(conn, acct, [msg("a", "2026-09-10T09:00:00",
                                           importance=1)])
    row = conn.execute("SELECT id FROM messages").fetchone()["id"]
    feeds.set_message(conn, row, importance_override=5)
    got = feeds.inbox(conn, min_importance=4)
    assert len(got) == 1 and got[0]["score"] == 5


def test_an_override_outside_the_scale_is_refused(conn, acct):
    feeds.upsert_messages(conn, acct, [msg("a", "2026-09-10T09:00:00")])
    row = conn.execute("SELECT id FROM messages").fetchone()["id"]
    for bad in (0, 6, 99):
        with pytest.raises(ValueError):
            feeds.set_message(conn, row, importance_override=bad)


def test_unknown_fields_are_refused(conn, acct):
    feeds.upsert_messages(conn, acct, [msg("a", "2026-09-10T09:00:00")])
    row = conn.execute("SELECT id FROM messages").fetchone()["id"]
    with pytest.raises(ValueError):
        feeds.set_message(conn, row, subject="rewritten")


def test_a_dirty_event_is_not_overwritten(conn, acct):
    feeds.upsert_events(conn, acct, [ev("a", "2026-09-10T09:00:00", title="Mine")])
    conn.execute("UPDATE events SET dirty=1, title='Edited'")
    conn.commit()
    written, skipped = feeds.upsert_events(
        conn, acct, [ev("a", "2026-09-10T09:00:00", title="Theirs")])
    assert (written, skipped) == (0, 1)
    assert feeds.agenda(conn, now="2026-09-10T08:00:00")[0]["title"] == "Edited"


# ── disconnecting ─────────────────────────────────────────────────────────
def test_disconnecting_an_account_takes_its_data_with_it(conn, acct):
    """Leaving mail behind after the account is gone would be a copy of
    someone's inbox with nothing left to explain it."""
    feeds.upsert_events(conn, acct, [ev("a", "2026-09-10T09:00:00")])
    feeds.upsert_messages(conn, acct, [msg("m", "2026-09-10T09:00:00")])
    settings.disconnect_google(conn, acct)
    assert conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"] == 0


# ── sync bookkeeping ──────────────────────────────────────────────────────
def test_sync_state_records_success_and_failure(conn, acct):
    feeds.note_sync(conn, "mail:x", status="ok", cursor="c1")
    feeds.note_sync(conn, "mail:x", status="error", error="invalid_grant")
    row = feeds.sync_status(conn)[0]
    assert row["last_status"] == "error"
    assert row["last_error"] == "invalid_grant"
    # The last success is kept, so "failing since" is answerable.
    assert row["last_ok_at"] is not None
    assert row["cursor"] == "c1"


def test_events_beyond_the_horizon_are_excluded_by_design(conn, acct):
    """Not a bug to work around: an agenda is about the near future."""
    feeds.upsert_events(conn, acct, [ev("far", "2099-01-01T09:00:00")])
    assert feeds.agenda(conn, days=90, now="2026-09-10T08:00:00") == []
    assert len(feeds.agenda(conn, days=36500, now="2026-09-10T08:00:00")) == 1


# ── the calendar grid's range query ───────────────────────────────────────
def test_events_between_includes_the_past(conn, acct):
    """A month grid must show days that already happened -- a month with the
    first fortnight blank is not a month."""
    feeds.upsert_events(conn, acct, [
        ev("old", "2026-09-02T09:00:00"),
        ev("new", "2026-09-20T09:00:00"),
    ])
    got = feeds.events_between(conn, "2026-09-01T00:00:00", "2026-09-30T23:59:59")
    assert {e["source_uid"] for e in got} == {"old", "new"}


def test_a_multi_day_event_appears_in_every_month_it_touches(conn, acct):
    feeds.upsert_events(conn, acct, [
        ev("trip", "2026-09-28T09:00:00", ends_at="2026-10-03T17:00:00"),
    ])
    sept = feeds.events_between(conn, "2026-09-01T00:00:00", "2026-09-30T23:59:59")
    octo = feeds.events_between(conn, "2026-10-01T00:00:00", "2026-10-31T23:59:59")
    assert [e["source_uid"] for e in sept] == ["trip"]
    assert [e["source_uid"] for e in octo] == ["trip"], \
        "an event spanning the boundary vanished from the second month"


def test_events_outside_the_range_are_excluded(conn, acct):
    feeds.upsert_events(conn, acct, [ev("far", "2026-12-01T09:00:00")])
    assert feeds.events_between(conn, "2026-09-01T00:00:00",
                                "2026-09-30T23:59:59") == []


def test_cancelled_events_stay_out_of_the_grid(conn, acct):
    feeds.upsert_events(conn, acct, [
        ev("gone", "2026-09-10T09:00:00", status="cancelled", deleted=1),
        ev("real", "2026-09-11T09:00:00"),
    ])
    got = feeds.events_between(conn, "2026-09-01T00:00:00", "2026-09-30T23:59:59")
    assert [e["source_uid"] for e in got] == ["real"]


def test_an_event_with_no_end_still_appears(conn, acct):
    """ends_at is nullable, and COALESCE on an empty string is not the same as
    on NULL -- both have to work or open-ended events disappear."""
    feeds.upsert_events(conn, acct, [ev("open", "2026-09-15T09:00:00", ends_at="")])
    got = feeds.events_between(conn, "2026-09-01T00:00:00", "2026-09-30T23:59:59")
    assert [e["source_uid"] for e in got] == ["open"]


# ── message bodies ────────────────────────────────────────────────────────
def test_a_body_is_fetched_once_and_then_remembered(conn, acct):
    """Gmail charges quota per request, and a body does not change."""
    feeds.upsert_messages(conn, acct, [msg("m1", "2026-09-01T09:00:00")])
    mid = conn.execute("SELECT id FROM messages").fetchone()[0]

    calls = []

    def fetch(account_id, source_uid):
        calls.append(source_uid)
        return "the body"

    text, blocks, cached = feeds.message_body(conn, mid, fetch)
    assert text == "the body" and cached is False
    text, blocks, cached = feeds.message_body(conn, mid, fetch)
    assert text == "the body" and cached is True
    assert calls == ["m1"], "fetched twice"


def test_an_empty_body_is_still_remembered(conn, acct):
    """An attachment-only message has no text. Without recording that, every
    view of it would hit Gmail again for the same nothing."""
    feeds.upsert_messages(conn, acct, [msg("m2", "2026-09-01T09:00:00")])
    mid = conn.execute("SELECT id FROM messages").fetchone()[0]
    calls = []
    feeds.message_body(conn, mid, lambda a, u: calls.append(u) or "")
    feeds.message_body(conn, mid, lambda a, u: calls.append(u) or "")
    assert len(calls) == 1


def test_no_fetcher_means_no_network(conn, acct):
    feeds.upsert_messages(conn, acct, [msg("m3", "2026-09-01T09:00:00")])
    mid = conn.execute("SELECT id FROM messages").fetchone()[0]
    assert feeds.message_body(conn, mid) == ("", [], False)


def test_an_unknown_message_raises(conn):
    with pytest.raises(KeyError):
        feeds.message_body(conn, "0" * 32)


def test_trash_and_spam_are_settable_and_mark_the_row_dirty(conn, acct):
    feeds.upsert_messages(conn, acct, [msg("m4", "2026-09-01T09:00:00")])
    mid = conn.execute("SELECT id FROM messages").fetchone()[0]
    feeds.set_message(conn, mid, trashed=1)
    r = conn.execute("SELECT trashed, dirty FROM messages").fetchone()
    assert r["trashed"] == 1 and r["dirty"] == 1

    conn.execute("UPDATE messages SET dirty=0")
    feeds.set_message(conn, mid, is_spam=1)
    r = conn.execute("SELECT is_spam, dirty FROM messages").fetchone()
    assert r["is_spam"] == 1 and r["dirty"] == 1


def test_an_importance_correction_does_not_mark_the_row_dirty(conn, acct):
    """Google has no idea what importance means, so there is nothing to send
    -- and a dirty flag would block Gmail's own updates until it cleared."""
    feeds.upsert_messages(conn, acct, [msg("m5", "2026-09-01T09:00:00")])
    mid = conn.execute("SELECT id FROM messages").fetchone()[0]
    feeds.set_message(conn, mid, importance_override=5)
    r = conn.execute("SELECT importance_override, dirty FROM messages").fetchone()
    assert r["importance_override"] == 5 and r["dirty"] == 0


def test_an_unknown_field_is_refused(conn, acct):
    feeds.upsert_messages(conn, acct, [msg("m6", "2026-09-01T09:00:00")])
    mid = conn.execute("SELECT id FROM messages").fetchone()[0]
    with pytest.raises(ValueError, match="cannot set"):
        feeds.set_message(conn, mid, body_text="injected")


def test_the_inbox_listing_never_carries_bodies(conn, acct):
    """m.* would ship up to 256 KB per message once bodies are cached, and
    the list does not show one."""
    feeds.upsert_messages(conn, acct, [msg("big", "2026-09-01T09:00:00")])
    mid = conn.execute("SELECT id FROM messages").fetchone()[0]
    conn.execute("UPDATE messages SET body_text=? WHERE id=?", ("x" * 5000, mid))
    conn.commit()

    rows = feeds.inbox(conn, min_importance=0)
    assert "body_text" not in rows[0]
    # But the list still knows whether one has been fetched.
    assert rows[0]["has_body"] == 1


def test_the_listing_carries_what_the_view_needs(conn, acct):
    feeds.upsert_messages(conn, acct, [msg("m", "2026-09-01T09:00:00",
                                           thread_id="t1")])
    row = feeds.inbox(conn, min_importance=0)[0]
    for field in ("thread_id", "trashed", "is_spam", "push_error", "score"):
        assert field in row, field


# ── editing events ────────────────────────────────────────────────────────
def test_a_new_event_is_local_and_pending(conn, acct):
    eid = feeds.create_event(conn, acct, title="Dentist",
                             starts_at="2026-09-15T14:00:00")
    r = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
    assert r["dirty"] == 1
    assert r["source_uid"].startswith("local:")
    assert r["ends_at"] == "2026-09-15T15:00:00", "no default duration"


def test_several_unsent_events_can_coexist(conn, acct):
    """Uniqueness is on (account, source_uid), so they cannot all be ''."""
    a = feeds.create_event(conn, acct, title="One", starts_at="2026-09-15T09:00:00")
    b = feeds.create_event(conn, acct, title="Two", starts_at="2026-09-15T10:00:00")
    uids = [r[0] for r in conn.execute("SELECT source_uid FROM events")]
    assert a != b and len(set(uids)) == 2


def test_an_offset_is_converted_to_utc(conn, acct):
    """The browser sends local time; the table holds UTC."""
    eid = feeds.create_event(conn, acct, title="Call",
                             starts_at="2026-09-15T14:00:00-05:00")
    r = conn.execute("SELECT starts_at FROM events WHERE id=?", (eid,)).fetchone()
    assert r["starts_at"] == "2026-09-15T19:00:00"


def test_an_all_day_event_ends_on_the_next_midnight(conn, acct):
    """Google's end is exclusive. Storing it inclusively paints an extra cell
    and sends a different day than the one shown."""
    eid = feeds.create_event(conn, acct, title="Holiday", all_day=True,
                             starts_at="2026-09-15T00:00:00")
    r = conn.execute("SELECT starts_at, ends_at FROM events WHERE id=?",
                     (eid,)).fetchone()
    assert r["starts_at"] == "2026-09-15T00:00:00"
    assert r["ends_at"] == "2026-09-16T00:00:00"


def test_an_end_before_the_start_is_refused(conn, acct):
    with pytest.raises(ValueError, match="end before it starts"):
        feeds.create_event(conn, acct, title="x",
                           starts_at="2026-09-15T14:00:00",
                           ends_at="2026-09-15T13:00:00")


def test_a_missing_start_is_refused(conn, acct):
    with pytest.raises(ValueError, match="start time"):
        feeds.create_event(conn, acct, title="x")


def test_an_unknown_account_is_refused(conn):
    with pytest.raises(KeyError):
        feeds.create_event(conn, "0" * 32, title="x",
                           starts_at="2026-09-15T14:00:00")


def test_an_unknown_event_field_is_refused(conn, acct):
    with pytest.raises(ValueError, match="cannot set"):
        feeds.create_event(conn, acct, starts_at="2026-09-15T14:00:00",
                           source_uid="injected")


def test_editing_marks_it_pending_again(conn, acct):
    eid = feeds.create_event(conn, acct, title="Old",
                             starts_at="2026-09-15T14:00:00")
    conn.execute("UPDATE events SET dirty=0, push_error='boom' WHERE id=?", (eid,))
    feeds.update_event(conn, eid, title="New")
    r = conn.execute("SELECT title, dirty, push_error FROM events").fetchone()
    assert r["title"] == "New" and r["dirty"] == 1 and r["push_error"] == ""


def test_switching_to_all_day_reshapes_times_it_did_not_mention(conn, acct):
    """Times are re-derived from the merged state, not the patch."""
    eid = feeds.create_event(conn, acct, title="x",
                             starts_at="2026-09-15T14:00:00")
    feeds.update_event(conn, eid, all_day=True)
    r = conn.execute("SELECT starts_at, ends_at FROM events").fetchone()
    assert r["starts_at"].endswith("T00:00:00")
    assert r["ends_at"] == "2026-09-16T00:00:00"


def test_a_partial_edit_keeps_the_rest(conn, acct):
    eid = feeds.create_event(conn, acct, title="Keep", location="Hill St",
                             starts_at="2026-09-15T14:00:00")
    feeds.update_event(conn, eid, title="Changed")
    r = conn.execute("SELECT title, location, starts_at FROM events").fetchone()
    assert r["title"] == "Changed" and r["location"] == "Hill St"
    assert r["starts_at"] == "2026-09-15T14:00:00"


def test_deleting_keeps_the_row_until_google_is_told(conn, acct):
    """Removing it outright would work locally and leave the event in the
    calendar for ever."""
    eid = feeds.create_event(conn, acct, title="x",
                             starts_at="2026-09-15T14:00:00")
    feeds.delete_event(conn, eid)
    r = conn.execute("SELECT pending_delete, dirty FROM events").fetchone()
    assert r["pending_delete"] == 1 and r["dirty"] == 1


def test_a_deleted_event_leaves_the_grid_at_once(conn, acct):
    eid = feeds.create_event(conn, acct, title="Gone",
                             starts_at="2026-09-15T14:00:00")
    assert len(feeds.events_between(conn, "2026-09-01T00:00:00",
                                    "2026-09-30T23:59:59")) == 1
    feeds.delete_event(conn, eid)
    assert feeds.events_between(conn, "2026-09-01T00:00:00",
                                "2026-09-30T23:59:59") == []


def test_a_tombstone_for_an_event_we_never_had_is_ignored(conn, acct):
    """Google keeps cancelled events and re-serves them. Without this, every
    deletion returns for ever as a hidden row."""
    feeds.upsert_events(conn, acct, [ev("gone", "2026-09-10T09:00:00", deleted=1)])
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_a_tombstone_for_an_event_we_do_have_still_deletes_it(conn, acct):
    """The other half: a deletion made in Google must reach the agenda rather
    than leaving a stale copy on it."""
    feeds.upsert_events(conn, acct, [ev("real", "2026-09-10T09:00:00")])
    assert len(feeds.events_between(conn, "2026-09-01T00:00:00",
                                    "2026-09-30T23:59:59")) == 1
    feeds.upsert_events(conn, acct, [ev("real", "2026-09-10T09:00:00", deleted=1)])
    assert feeds.events_between(conn, "2026-09-01T00:00:00",
                                "2026-09-30T23:59:59") == []


# ── repeats and reminders ─────────────────────────────────────────────────
def test_a_repeat_becomes_a_real_rrule(conn, acct):
    eid = feeds.create_event(conn, acct, title="Standup",
                             starts_at="2026-09-15T09:00:00",
                             recurrence="weekly")
    r = conn.execute("SELECT recurrence FROM events WHERE id=?", (eid,)).fetchone()
    assert r["recurrence"] == "RRULE:FREQ=WEEKLY"


def test_an_arbitrary_rule_from_the_browser_is_refused(conn, acct):
    """A rule is posted straight into someone's calendar. The set worth
    offering is small and knowable, so it is a list rather than a parser."""
    with pytest.raises(ValueError, match="not one of the options"):
        feeds.create_event(conn, acct, title="x",
                           starts_at="2026-09-15T09:00:00",
                           recurrence="FREQ=SECONDLY;COUNT=999999")


def test_a_rule_google_gave_us_round_trips(conn, acct):
    """Editing an event Google created must not destroy its rule."""
    eid = feeds.create_event(conn, acct, title="x",
                             starts_at="2026-09-15T09:00:00")
    conn.execute("UPDATE events SET recurrence=? WHERE id=?",
                 ("RRULE:FREQ=MONTHLY;BYMONTHDAY=3", eid))
    conn.commit()
    feeds.update_event(conn, eid, title="renamed")
    r = conn.execute("SELECT recurrence FROM events WHERE id=?", (eid,)).fetchone()
    assert r["recurrence"] == "RRULE:FREQ=MONTHLY;BYMONTHDAY=3"


def test_a_reminder_is_stored_in_minutes(conn, acct):
    eid = feeds.create_event(conn, acct, title="x",
                             starts_at="2026-09-15T09:00:00",
                             reminder_minutes=30)
    r = conn.execute("SELECT reminder_minutes FROM events WHERE id=?",
                     (eid,)).fetchone()
    assert r["reminder_minutes"] == 30


def test_no_reminder_choice_means_the_calendar_default(conn, acct):
    """-1 is a real choice and not an absent one."""
    eid = feeds.create_event(conn, acct, title="x",
                             starts_at="2026-09-15T09:00:00")
    r = conn.execute("SELECT reminder_minutes FROM events WHERE id=?",
                     (eid,)).fetchone()
    assert r["reminder_minutes"] == -1


def test_an_unlisted_reminder_is_refused(conn, acct):
    with pytest.raises(ValueError, match="not one of the options"):
        feeds.create_event(conn, acct, title="x",
                           starts_at="2026-09-15T09:00:00",
                           reminder_minutes=7)


def test_a_raw_rule_cannot_be_posted_even_though_stored_ones_survive(conn, acct):
    """The round-trip path must not become a way in. Google's rules are
    richer than the list; a caller's are not allowed to be."""
    with pytest.raises(ValueError, match="not one of the options"):
        feeds.create_event(conn, acct, title="x",
                           starts_at="2026-09-15T09:00:00",
                           recurrence="RRULE:FREQ=SECONDLY")
    eid = feeds.create_event(conn, acct, title="y",
                             starts_at="2026-09-15T09:00:00")
    conn.execute("UPDATE events SET recurrence='RRULE:FREQ=SECONDLY'"
                 " WHERE id=?", (eid,))
    conn.commit()
    with pytest.raises(ValueError, match="not one of the options"):
        feeds.update_event(conn, eid, recurrence="RRULE:FREQ=SECONDLY")


def test_an_event_google_owns_cannot_be_edited(conn, acct):
    """A save that appears to work and reverts three minutes later is worse
    than one that says no."""
    feeds.upsert_events(conn, acct, [
        ev("bday", "2026-10-04T00:00:00", all_day=1, event_type="birthday")])
    eid = conn.execute("SELECT id FROM events").fetchone()[0]
    with pytest.raises(ValueError, match="birthday"):
        feeds.update_event(conn, eid, title="Renamed")


def test_an_ordinary_event_is_still_editable(conn, acct):
    feeds.upsert_events(conn, acct, [ev("normal", "2026-10-04T09:00:00")])
    eid = conn.execute("SELECT id FROM events").fetchone()[0]
    feeds.update_event(conn, eid, title="Renamed")
    assert conn.execute("SELECT title FROM events").fetchone()[0] == "Renamed"


def test_the_event_type_survives_a_sync(conn, acct):
    feeds.upsert_events(conn, acct, [
        ev("b", "2026-10-04T00:00:00", event_type="birthday")])
    assert conn.execute("SELECT event_type FROM events").fetchone()[0] == "birthday"
