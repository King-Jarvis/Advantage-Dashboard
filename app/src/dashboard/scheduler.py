"""The background loop that keeps the dashboard in step with Google.

This was meant to be n8n's job, and until n8n exists it was nobody's: push
only ran inside sync.run, and sync.run only ran when something called it. So
an archived message stayed archived locally for ever, new mail never arrived,
and the application quietly became a snapshot of whenever it was last poked by
hand. The push path was correct and simply never fired.

Two different rhythms, deliberately:

An edit is pushed at once. Archiving something and watching it sit there for
half a minute reads as broken, and the push is cheap -- a label change is one
small request against a quota measured in thousands.

A pull happens on a timer. Fetching mail is expensive by comparison, and
nothing is gained by asking Google for the same forty messages every few
seconds.

The thread never dies. Anything it throws is logged and the loop continues,
because a background worker that exits silently leaves an application that
looks fine and has stopped working -- which is the exact failure this module
was written to end.
"""
import threading

from . import push, settings, storage, sync

# Below this, a poll costs more in quota than it buys in freshness.
MIN_INTERVAL = 60
DEFAULT_INTERVAL = 180

_thread = None
_wake = threading.Event()
_stop = threading.Event()


def interval(conn):
    try:
        value = int(settings.get(conn, "mail_poll_seconds") or 0)
    except (TypeError, ValueError):
        value = 0
    return max(MIN_INTERVAL, value or DEFAULT_INTERVAL)


def nudge():
    """An edit is waiting. Push it now rather than at the next tick."""
    _wake.set()


def _once(conn, woken):
    if not settings.list_google_accounts(conn):
        return
    if woken:
        # Only the push: the edit is the thing that cannot wait, and pulling
        # here would make every click cost a mailbox fetch.
        push.run(conn)
    else:
        sync.run(conn)


def _loop():
    while not _stop.is_set():
        conn = None
        try:
            conn = storage.connect()
            wait_for = interval(conn)
        except Exception:
            wait_for = DEFAULT_INTERVAL
        woken = _wake.wait(timeout=wait_for)
        if _stop.is_set():
            return
        _wake.clear()
        try:
            _once(conn or storage.connect(), woken)
        except Exception as e:
            # Never propagate: a thread that dies takes the whole feature with
            # it and says nothing.
            storage.log("scheduler: %s" % e)


def start():
    """Begin the loop. Safe to call twice; the second call does nothing."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return _thread
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="sync", daemon=True)
    _thread.start()
    storage.log("scheduler started")
    return _thread


def stop():
    _stop.set()
    _wake.set()
