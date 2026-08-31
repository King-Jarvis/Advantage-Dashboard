"""Database connection and data root.

Data never lives inside the checkout. The root is chosen once at startup:
``--data PATH``, else ``$DASHBOARD_DATA``, else ``~/.dashboard``. The repository
is public, so a stray database file inside it would be a disclosure, not just
untidiness.
"""

import os
import sqlite3
import threading
import time

DEFAULT_ROOT = os.path.join(os.path.expanduser("~"), ".dashboard")

ROOT = DEFAULT_ROOT
DBPATH = os.path.join(DEFAULT_ROOT, "dashboard.db")
LOGFILE = os.path.join(DEFAULT_ROOT, "dashboard.log")

_log_lock = threading.Lock()
_local = threading.local()


def resolve_root(cli_value=None):
    return cli_value or os.environ.get("DASHBOARD_DATA") or DEFAULT_ROOT


def configure(root):
    global ROOT, DBPATH, LOGFILE
    ROOT = os.path.abspath(os.path.expanduser(root))
    DBPATH = os.path.join(ROOT, "dashboard.db")
    LOGFILE = os.path.join(ROOT, "dashboard.log")
    os.makedirs(ROOT, exist_ok=True)
    # The database holds mail, calendar and finances. Nothing else on the host
    # has any business reading it.
    try:
        os.chmod(ROOT, 0o700)
    except OSError:
        pass
    return ROOT


def log(message):
    line = "%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    with _log_lock:
        try:
            with open(LOGFILE, "a") as fh:
                fh.write(line)
        except OSError:
            pass


def connect(path=None):
    """A configured connection.

    Foreign keys are OFF by default in SQLite and are a per-connection
    setting, not a property of the file -- so every connection must enable
    them or the schema's references are decorative.
    """
    conn = sqlite3.connect(path or DBPATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # Without a busy timeout, a concurrent writer raises "database is locked"
    # immediately rather than waiting for a lock that is nearly always brief.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def get_conn():
    """One connection per thread. sqlite3 objects are not thread-safe."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _local.conn = connect()
    return conn
