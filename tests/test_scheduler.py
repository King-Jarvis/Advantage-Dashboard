"""The background loop.

Its entire job is to fire without being asked, so the tests are about that:
it wakes, it does the right amount of work, and it never dies quietly.
"""
import threading
import time

import pytest

from dashboard import scheduler, settings


@pytest.fixture(autouse=True)
def quiet():
    yield
    scheduler.stop()
    scheduler._stop.clear()
    scheduler._wake.clear()


def test_the_interval_has_a_floor(conn):
    """Below a minute a poll costs more in quota than it buys in freshness."""
    settings.set_(conn, "mail_poll_seconds", 5)
    assert scheduler.interval(conn) >= scheduler.MIN_INTERVAL


def test_a_sensible_setting_is_respected(conn):
    settings.set_(conn, "mail_poll_seconds", 600)
    assert scheduler.interval(conn) == 600


def test_a_missing_setting_falls_back(conn):
    settings.set_(conn, "mail_poll_seconds", 0)
    assert scheduler.interval(conn) == scheduler.DEFAULT_INTERVAL


def test_an_edit_pushes_without_pulling(conn, monkeypatch):
    """The click is what cannot wait. Pulling here would make every archive
    cost a mailbox fetch."""
    calls = []
    monkeypatch.setattr(scheduler.push, "run", lambda c, **k: calls.append("push"))
    monkeypatch.setattr(scheduler.sync, "run", lambda c, **k: calls.append("sync"))
    monkeypatch.setattr(scheduler.settings, "list_google_accounts",
                        lambda c: [{"id": "a"}])
    scheduler._once(conn, woken=True)
    assert calls == ["push"]


def test_a_tick_does_the_full_sync(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler.push, "run", lambda c, **k: calls.append("push"))
    monkeypatch.setattr(scheduler.sync, "run", lambda c, **k: calls.append("sync"))
    monkeypatch.setattr(scheduler.settings, "list_google_accounts",
                        lambda c: [{"id": "a"}])
    scheduler._once(conn, woken=False)
    assert calls == ["sync"]


def test_nothing_happens_without_a_connected_account(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler.push, "run", lambda c, **k: calls.append("push"))
    monkeypatch.setattr(scheduler.sync, "run", lambda c, **k: calls.append("sync"))
    monkeypatch.setattr(scheduler.settings, "list_google_accounts", lambda c: [])
    scheduler._once(conn, woken=True)
    scheduler._once(conn, woken=False)
    assert calls == []


def test_a_nudge_wakes_the_loop_promptly(monkeypatch):
    """The whole point: an edit must not wait for the next tick."""
    fired = threading.Event()
    monkeypatch.setattr(scheduler, "interval", lambda c: 3600)
    monkeypatch.setattr(scheduler, "_once",
                        lambda conn, woken: fired.set() if woken else None)
    scheduler.start()
    time.sleep(0.1)
    scheduler.nudge()
    assert fired.wait(timeout=3), "a nudge did not wake the loop"


def test_a_failure_does_not_kill_the_thread(monkeypatch):
    """A worker that exits silently leaves an application that looks fine and
    has stopped working."""
    tries = []

    def boom(conn, woken):
        tries.append(1)
        raise RuntimeError("nope")

    monkeypatch.setattr(scheduler, "interval", lambda c: 3600)
    monkeypatch.setattr(scheduler, "_once", boom)
    scheduler.start()
    time.sleep(0.1)
    scheduler.nudge()
    time.sleep(0.3)
    scheduler.nudge()
    time.sleep(0.3)
    assert len(tries) >= 2, "the loop stopped after the first failure"
    assert scheduler._thread.is_alive()


def test_starting_twice_makes_one_thread(monkeypatch):
    monkeypatch.setattr(scheduler, "interval", lambda c: 3600)
    monkeypatch.setattr(scheduler, "_once", lambda conn, woken: None)
    first = scheduler.start()
    second = scheduler.start()
    assert first is second
