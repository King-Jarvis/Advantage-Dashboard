"""Reading Google, without Google.

Every test here stubs the HTTP seam. The point is not to prove Google's API
works; it is to prove that what comes back is mapped correctly, that a stale
token is retried exactly once, and that one bad account or one bad message
does not take the rest down with it.
"""
import time

import pytest

from dashboard import crypt, feeds, google_api, settings, sync


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


# ── time normalisation ────────────────────────────────────────────────────
def test_offsets_become_utc():
    # 11:00 in London in summer is 10:00 UTC. Storing the offset would make
    # ordering by starts_at wrong for anyone who travels.
    assert google_api._to_utc("2026-09-01T11:00:00+01:00") == "2026-09-01T10:00:00"
    assert google_api._to_utc("2026-09-01T10:00:00Z") == "2026-09-01T10:00:00"


def test_all_day_and_rubbish_survive():
    assert google_api._to_utc("2026-09-01") == "2026-09-01T00:00:00"
    assert google_api._to_utc("") == ""
    assert google_api._to_utc("not a date")          # returns something, no raise


# ── header parsing ────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,name,addr", [
    ('Ada Lovelace <ada@example.com>', "Ada Lovelace", "ada@example.com"),
    ('"Babbage, C" <c@example.com>', "Babbage, C", "c@example.com"),
    ('plain@example.com', "plain@example.com", "plain@example.com"),
    ('<solo@example.com>', "solo@example.com", "solo@example.com"),
])
def test_from_header(raw, name, addr):
    assert google_api._split_from(raw) == (name, addr)


def test_importance_always_has_a_reason():
    # A score with no reason can only be trusted blindly or ignored.
    for labels in ([], ["IMPORTANT"], ["UNREAD"], ["STARRED"],
                   ["CATEGORY_PROMOTIONS"], ["IMPORTANT", "UNREAD"]):
        for bulk in (False, True):
            for direct in (False, True):
                score, reason = google_api._baseline(labels, bulk, direct)
                assert 1 <= score <= 5
                assert reason


def test_a_newsletter_ranks_below_a_person(conn):
    """The signal that matters: List-Unsubscribe means no human typed it."""
    newsletter = google_api._baseline(["UNREAD"], bulk=True)[0]
    person = google_api._baseline(["UNREAD"], bulk=False, direct=True)[0]
    assert newsletter < person


def test_addressed_to_you_outranks_merely_unread():
    assert google_api._baseline(["UNREAD"], direct=True)[0] > \
           google_api._baseline(["UNREAD"])[0]


def test_a_promotional_mailing_is_the_floor():
    assert google_api._baseline(["UNREAD", "CATEGORY_PROMOTIONS"],
                                bulk=True)[0] == 1


def test_starring_something_outranks_everything():
    starred = google_api._baseline(["STARRED", "CATEGORY_PROMOTIONS"],
                                   bulk=True)[0]
    assert starred == 5, "an explicit human signal was overridden by a label"


def test_the_scores_actually_spread(conn):
    """A ranking where everything scores alike ranks nothing."""
    cases = [
        (["UNREAD", "CATEGORY_PROMOTIONS"], True, False),
        (["UNREAD"], True, False),
        (["UNREAD"], False, False),
        (["UNREAD"], False, True),
        (["STARRED"], False, False),
    ]
    scores = [google_api._baseline(lab, b, d)[0] for lab, b, d in cases]
    assert len(set(scores)) >= 4, "scores bunched together: %r" % (scores,)
    assert scores == sorted(scores), "not monotonic: %r" % (scores,)


# ── access tokens ─────────────────────────────────────────────────────────
def test_cached_token_is_reused(conn, acct, monkeypatch):
    calls = []
    monkeypatch.setattr(google_api.auth_google, "refresh",
                        lambda *a, **k: calls.append(1) or {})
    assert google_api.access_token(conn, acct) == "FAKE-ACCESS"
    assert calls == [], "refreshed a token that had not expired"


def test_expired_token_refreshes_and_is_stored(conn, acct, monkeypatch):
    conn.execute("UPDATE google_accounts SET expires_at=? WHERE id=?",
                 (time.time() - 10, acct))
    monkeypatch.setattr(google_api.auth_google, "refresh",
                        lambda *a, **k: {"access_token": "NEW", "expires_in": 3600})
    assert google_api.access_token(conn, acct) == "NEW"
    # Stored encrypted, and reused next time without another refresh.
    raw = conn.execute("SELECT access_token FROM google_accounts WHERE id=?",
                       (acct,)).fetchone()[0]
    assert "NEW" not in raw, "access token written to the database in clear"
    assert google_api.access_token(conn, acct) == "NEW"


def test_missing_refresh_token_says_so(conn, acct, monkeypatch):
    conn.execute("UPDATE google_accounts SET refresh_token='', expires_at=0"
                 " WHERE id=?", (acct,))
    with pytest.raises(google_api.GoogleError, match="reconnect"):
        google_api.access_token(conn, acct)


def test_one_401_is_retried_twice_is_not(conn, acct, monkeypatch):
    monkeypatch.setattr(google_api.auth_google, "refresh",
                        lambda *a, **k: {"access_token": "NEW", "expires_in": 3600})
    seen = []

    def once(url, token, params=None):
        seen.append(token)
        if len(seen) == 1:
            raise google_api._Unauthorized()
        return {"items": []}

    monkeypatch.setattr(google_api, "_get", once)
    google_api._get_retrying(conn, acct, "u")
    assert len(seen) == 2 and seen[1] == "NEW"

    def always(url, token, params=None):
        raise google_api._Unauthorized()

    monkeypatch.setattr(google_api, "_get", always)
    with pytest.raises(google_api.GoogleError, match="reconnect"):
        google_api._get_retrying(conn, acct, "u")


# ── calendar mapping ──────────────────────────────────────────────────────
def _cal(monkeypatch, items):
    monkeypatch.setattr(google_api, "_get_retrying",
                        lambda *a, **k: {"items": items})


def test_cancelled_events_come_through_as_deleted(conn, acct, monkeypatch):
    # Dropping them would leave the stale copy on the agenda forever.
    _cal(monkeypatch, [
        {"id": "a", "summary": "Standup", "status": "confirmed",
         "start": {"dateTime": "2026-09-01T09:00:00Z"},
         "end": {"dateTime": "2026-09-01T09:15:00Z"}},
        {"id": "b", "summary": "Gone", "status": "cancelled",
         "start": {"dateTime": "2026-09-01T10:00:00Z"}, "end": {}},
    ])
    out = google_api.fetch_events(conn, acct)
    assert [e["deleted"] for e in out] == [0, 1]


def test_all_day_flagged_and_untitled_named(conn, acct, monkeypatch):
    _cal(monkeypatch, [{"id": "c", "status": "confirmed",
                        "start": {"date": "2026-09-02"},
                        "end": {"date": "2026-09-03"}}])
    e = google_api.fetch_events(conn, acct)[0]
    assert e["all_day"] == 1 and e["title"] == "(no title)"


def test_event_without_a_start_is_skipped(conn, acct, monkeypatch):
    _cal(monkeypatch, [{"id": "d", "status": "confirmed", "start": {}, "end": {}}])
    assert google_api.fetch_events(conn, acct) == []


def test_fetched_events_satisfy_upsert(conn, acct, monkeypatch):
    """The mapper's output must be what the writer expects -- these two
    drifting apart is exactly the class of bug that shows up as an empty
    screen and no error."""
    _cal(monkeypatch, [{"id": "e1", "summary": "Dentist", "status": "confirmed",
                        "location": "Hill St",
                        "start": {"dateTime": "2026-09-01T11:00:00+01:00"},
                        "end": {"dateTime": "2026-09-01T11:30:00+01:00"}}])
    written, _ = feeds.upsert_events(conn, acct, google_api.fetch_events(conn, acct))
    assert written == 1
    row = conn.execute("SELECT starts_at, location FROM events").fetchone()
    assert row["starts_at"] == "2026-09-01T10:00:00" and row["location"] == "Hill St"


# ── mail mapping ──────────────────────────────────────────────────────────
def _mail(monkeypatch, listing, bodies):
    def fake(conn, aid, url, params=None):
        if url == google_api.GMAIL_LIST:
            return listing
        key = url.rsplit("/", 1)[-1]
        got = bodies[key]
        if isinstance(got, Exception):
            raise got
        return got
    monkeypatch.setattr(google_api, "_get_retrying", fake)


def test_a_newsletter_and_a_person_get_different_scores(conn, acct, monkeypatch):
    """End to end through the mapper, with the headers Gmail actually sends."""
    def hdrs(pairs):
        return [{"name": k, "value": v} for k, v in pairs]
    _mail(monkeypatch, {"messages": [{"id": "n1"}, {"id": "p1"}]}, {
        "n1": {"id": "n1", "labelIds": ["INBOX", "UNREAD"],
               "internalDate": "1756713600000",
               "payload": {"headers": hdrs([
                   ("From", "News <news@example.com>"),
                   ("To", "a@example.com"),
                   ("List-Unsubscribe", "<https://example.com/u>"),
                   ("Subject", "Weekly digest")])}},
        "p1": {"id": "p1", "labelIds": ["INBOX", "UNREAD"],
               "internalDate": "1756713600000",
               "payload": {"headers": hdrs([
                   ("From", "Sam <sam@example.com>"),
                   ("To", "a@example.com"),
                   ("Subject", "are you free thursday")])}},
    })
    by_id = {m["source_uid"]: m for m in google_api.fetch_messages(conn, acct)}
    assert by_id["p1"]["importance"] > by_id["n1"]["importance"]
    assert "mailing" in by_id["n1"]["reason"]


def test_one_unreadable_message_does_not_lose_the_others(conn, acct, monkeypatch):
    _mail(monkeypatch,
          {"messages": [{"id": "m1"}, {"id": "m2"}]},
          {"m1": google_api.GoogleError("boom"),
           "m2": {"id": "m2", "threadId": "t", "labelIds": ["INBOX", "UNREAD"],
                  "internalDate": "1756713600000", "snippet": "hi",
                  "payload": {"headers": [
                      {"name": "From", "value": "Ada <ada@example.com>"},
                      {"name": "Subject", "value": "Hello"}]}}})
    out = google_api.fetch_messages(conn, acct)
    assert [m["source_uid"] for m in out] == ["m2"]
    assert out[0]["sender_email"] == "ada@example.com"
    assert out[0]["archived"] == 0 and out[0]["is_unread"] == 1


def test_message_outside_inbox_is_archived(conn, acct, monkeypatch):
    _mail(monkeypatch, {"messages": [{"id": "m3"}]},
          {"m3": {"id": "m3", "labelIds": ["CATEGORY_PROMOTIONS"],
                  "internalDate": "1756713600000",
                  "payload": {"headers": []}}})
    m = google_api.fetch_messages(conn, acct)[0]
    assert m["archived"] == 1 and m["subject"] == "(no subject)"


def test_fetched_messages_satisfy_upsert(conn, acct, monkeypatch):
    _mail(monkeypatch, {"messages": [{"id": "m4"}]},
          {"m4": {"id": "m4", "labelIds": ["INBOX", "IMPORTANT", "UNREAD"],
                  "internalDate": "1756713600000", "snippet": "s",
                  "payload": {"headers": [
                      {"name": "From", "value": "B <b@example.com>"},
                      {"name": "Subject", "value": "Invoice"}]}}})
    written, _ = feeds.upsert_messages(conn, acct,
                                       google_api.fetch_messages(conn, acct))
    assert written == 1
    row = conn.execute("SELECT importance, reason, subject FROM messages").fetchone()
    assert row["subject"] == "Invoice" and 1 <= row["importance"] <= 5
    assert row["reason"], "stored a score with no reason"


def test_no_message_ids_is_not_an_error(conn, acct, monkeypatch):
    _mail(monkeypatch, {}, {})
    assert google_api.fetch_messages(conn, acct) == []


# ── orchestration ─────────────────────────────────────────────────────────
def test_no_connected_account_reports_rather_than_raises(conn):
    out = sync.run(conn)
    assert out["ok"] is False and "no connected" in out["error"]
    assert feeds.sync_status(conn)


def test_one_failing_account_does_not_stop_the_other(conn, acct, monkeypatch):
    second = settings.save_google_account(conn, "sub-2", "b@example.com",
                                          "FAKE-REFRESH-2", "FAKE-ACCESS-2",
                                          time.time() + 3600, "s")

    def per_account(conn_, aid, **kw):
        if aid == acct:
            raise google_api.GoogleError("revoked")
        return []

    monkeypatch.setattr(google_api, "fetch_events", per_account)
    monkeypatch.setattr(google_api, "fetch_messages",
                        lambda *a, **k: [])
    out = sync.run(conn)
    assert out["ok"] is False
    by_ok = {r["account"]: r["ok"] for r in out["results"]}
    assert by_ok == {"a@example.com": False, "b@example.com": True}
    # The failure is recorded against the account that had it, so the
    # settings page can name it.
    err = conn.execute("SELECT last_error FROM google_accounts WHERE id=?",
                       (acct,)).fetchone()[0]
    assert "revoked" in err
    assert conn.execute("SELECT last_error FROM google_accounts WHERE id=?",
                        (second,)).fetchone()[0] == ""
    assert feeds.sync_status(conn)


def test_all_succeeding_reports_ok(conn, acct, monkeypatch):
    monkeypatch.setattr(google_api, "fetch_events", lambda *a, **k: [])
    monkeypatch.setattr(google_api, "fetch_messages", lambda *a, **k: [])
    out = sync.run(conn)
    assert out["ok"] is True and len(out["results"]) == 1


def test_local_edits_are_reported_as_held(conn, acct, monkeypatch):
    """A held count that stays high means a push is not happening -- worth
    surfacing rather than hiding."""
    feeds.upsert_messages(conn, acct, [
        {"source_uid": "m9", "received_at": "2026-09-01T10:00:00",
         "subject": "x"}])
    mid = conn.execute("SELECT id FROM messages").fetchone()[0]
    feeds.set_message(conn, mid, archived=1)
    monkeypatch.setattr(google_api, "fetch_events", lambda *a, **k: [])
    monkeypatch.setattr(google_api, "fetch_messages", lambda *a, **k: [
        {"source_uid": "m9", "received_at": "2026-09-01T10:00:00",
         "subject": "x"}])
    out = sync.run(conn)
    assert out["results"][0]["messages_held"] == 1


# ── what a 403 tells you ──────────────────────────────────────────────────
def _api_error(payload, code=403):
    import io
    import json as _json
    import urllib.error
    return urllib.error.HTTPError("https://gmail.googleapis.com/x", code,
                                  "Forbidden", {},
                                  io.BytesIO(_json.dumps(payload).encode()))


DISABLED = {"error": {"code": 403, "status": "PERMISSION_DENIED",
                      "message": "Gmail API has not been used in project "
                                 "123456789 before or it is disabled",
                      "errors": [{"reason": "accessNotConfigured"}]}}


def test_a_disabled_api_says_which_one_and_where_to_enable_it(monkeypatch):
    """The most common wall when connecting a fresh Cloud project, and '403'
    does not hint at it even slightly."""
    def boom(req, timeout=None):
        raise _api_error(DISABLED)
    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(google_api.GoogleError) as e:
        google_api._get(google_api.GMAIL_LIST, "tok")
    msg = str(e.value)
    assert "Gmail API is not enabled" in msg
    assert "console.cloud.google.com/apis/library/gmail.googleapis.com" in msg


def test_the_calendar_variant_names_the_calendar_api(monkeypatch):
    def boom(req, timeout=None):
        raise _api_error(DISABLED)
    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(google_api.GoogleError) as e:
        google_api._get(google_api.CAL_URL, "tok")
    msg = str(e.value)
    assert "Calendar API is not enabled" in msg
    assert "calendar-json.googleapis.com" in msg


def test_googles_message_is_never_surfaced(monkeypatch):
    """It echoes the request back, project number included."""
    def boom(req, timeout=None):
        raise _api_error(DISABLED)
    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(google_api.GoogleError) as e:
        google_api._get(google_api.GMAIL_LIST, "tok")
    assert "123456789" not in str(e.value)


def test_missing_scopes_point_at_reconnecting(monkeypatch):
    def boom(req, timeout=None):
        raise _api_error({"error": {"errors": [
            {"reason": "insufficientPermissions"}]}})
    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(google_api.GoogleError, match="Reconnect"):
        google_api._get(google_api.GMAIL_LIST, "tok")


def test_rate_limiting_says_it_will_retry(monkeypatch):
    def boom(req, timeout=None):
        raise _api_error({"error": {"errors": [{"reason": "rateLimitExceeded"}]}},
                         code=429)
    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(google_api.GoogleError, match="rate limit"):
        google_api._get(google_api.GMAIL_LIST, "tok")


def test_an_unknown_reason_still_reports_usefully(monkeypatch):
    def boom(req, timeout=None):
        raise _api_error({"error": {"errors": [{"reason": "somethingNew"}]}})
    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(google_api.GoogleError, match="somethingNew"):
        google_api._get(google_api.GMAIL_LIST, "tok")


def test_a_401_is_still_a_token_problem_not_a_403_message(monkeypatch):
    """401 must stay on the refresh-and-retry path, not become prose."""
    def boom(req, timeout=None):
        raise _api_error({"error": {}}, code=401)
    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(google_api._Unauthorized):
        google_api._get(google_api.GMAIL_LIST, "tok")


def test_list_parameters_are_repeated_not_stringified(monkeypatch):
    """Gmail wants metadataHeaders=A&metadataHeaders=B. Sent as a repr it is
    ignored, and the headers come back as Gmail's default set -- which quietly
    excludes the ones the classifier depends on."""
    seen = {}

    class FakeResp:
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake(req, timeout=None):
        seen["url"] = req.full_url
        return FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake)
    google_api._get("https://x/y", "tok",
                    {"metadataHeaders": ["From", "List-Unsubscribe"]})
    assert "metadataHeaders=From" in seen["url"]
    assert "metadataHeaders=List-Unsubscribe" in seen["url"]
    assert "%5B" not in seen["url"], "a list was serialised as its repr"


# ── machine mail vs a person ──────────────────────────────────────────────
@pytest.mark.parametrize("addr", [
    "no-reply@x.com", "noreply@x.com", "do_not_reply@x.com",
    "auto-confirm@amazon.com", "shipment-tracking@amazon.com",
    "order-update@amazon.com", "notification@x.com", "mailer-daemon@x.com",
])
def test_machine_addresses_are_recognised(addr):
    assert google_api._is_machine(addr), addr


@pytest.mark.parametrize("addr", [
    "sam@example.com", "j.smith@work.co.uk", "contact@x.com",
    "hello@x.com", "support@x.com",
])
def test_human_addresses_are_not(addr):
    assert not google_api._is_machine(addr), addr


def test_a_dispatch_note_does_not_outrank_a_friend():
    """The ranking an inbox exists to fix. An order confirmation is addressed
    to you personally and is still not someone asking you for something."""
    robot = google_api._baseline(["UNREAD"], direct=True, machine=True)[0]
    friend = google_api._baseline(["UNREAD"], direct=True, machine=False)[0]
    assert robot < friend


def test_a_machine_notice_is_still_visible():
    """Lower than a person, but not buried with the promotions."""
    score, reason = google_api._baseline(["UNREAD"], direct=True, machine=True)
    assert score == 3 and "automated" in reason


def test_gmails_important_label_does_not_promote_a_robot():
    assert google_api._baseline(["IMPORTANT", "UNREAD"], machine=True)[0] == 3


def test_starring_a_robot_still_wins():
    """An explicit human signal outranks every inference."""
    assert google_api._baseline(["STARRED"], machine=True)[0] == 5


def test_a_missing_encryption_key_is_not_reported_as_a_missing_token(
        conn, acct, monkeypatch):
    """One means reconnect the account; the other means reconnecting would
    store a token that cannot be read back either."""
    conn.execute("UPDATE google_accounts SET expires_at=0 WHERE id=?", (acct,))
    monkeypatch.setattr(google_api.crypt, "available", lambda: False)
    with pytest.raises(google_api.GoogleError, match="encryption key"):
        google_api.access_token(conn, acct)
