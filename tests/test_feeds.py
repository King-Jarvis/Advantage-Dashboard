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
