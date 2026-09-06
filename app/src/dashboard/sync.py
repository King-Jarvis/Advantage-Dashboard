"""Pulling Google into the local tables.

This is the function a workflow engine calls. It takes no credentials and
returns no tokens -- the caller says "sync now" and learns how many rows
moved. That is the whole contract, and it is deliberately narrow: the less a
scheduler is trusted with, the less a compromised scheduler costs.

One account failing must not stop the others. A household with a personal and
a work account should not lose the personal calendar because the work one had
its access revoked.
"""
from . import feeds, google_api, push, settings


def _one(conn, account, days_ahead, mail_query):
    aid = account["id"]
    events = google_api.fetch_events(conn, aid, days_ahead=days_ahead)
    ev_written, ev_skipped = feeds.upsert_events(conn, aid, events)
    messages = google_api.fetch_messages(conn, aid, query=mail_query)
    ms_written, ms_skipped = feeds.upsert_messages(conn, aid, messages)
    settings.note_sync(conn, aid, "")
    return {
        "account": account.get("email", ""),
        "ok": True,
        "events": ev_written, "events_held": ev_skipped,
        "messages": ms_written, "messages_held": ms_skipped,
    }


def run(conn, account_id=None, days_ahead=21,
        mail_query="-in:chats newer_than:14d"):
    """Sync every connected account, or just one.

    Returns a per-account summary. 'held' counts rows left alone because they
    carry an unpushed local edit -- worth reporting rather than hiding, since
    a number that stays high means a push is not happening.
    """
    accounts = settings.list_google_accounts(conn)
    if account_id:
        accounts = [a for a in accounts if a["id"] == account_id]
    if not accounts:
        feeds.note_sync(conn, "google", "error", "no connected account")
        return {"ok": False, "error": "no connected Google account",
                "results": []}

    # Push before pull, always. The other order means a poll overwrites the
    # local copy of a row whose edit has not been sent yet -- and because a
    # dirty row is skipped by the writer, the edit would then sit for ever
    # against data that has already moved on. Pushing first lets the pull
    # confirm what we just did instead of fighting it.
    pushed = push.run(conn, account_id=account_id)

    results, failures = [], 0
    for account in accounts:
        try:
            results.append(_one(conn, account, days_ahead, mail_query))
        except Exception as e:
            failures += 1
            # The message is stored against the account so the settings page
            # can say which one is broken and why.
            settings.note_sync(conn, account["id"], str(e))
            results.append({"account": account.get("email", ""),
                            "ok": False, "error": str(e)})

    status = "ok" if failures == 0 else (
        "error" if failures == len(accounts) else "partial")
    feeds.note_sync(conn, "google", status,
                    "" if status == "ok" else
                    "%d of %d accounts failed" % (failures, len(accounts)))
    return {"ok": failures == 0, "results": results,
            "pushed": pushed["results"], "push_ok": pushed["ok"],
            "still_pending": push.pending_count(conn)}
