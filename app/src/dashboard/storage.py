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
    """Point the module at a data root and make sure it exists.

    Drops this thread's cached connection: after a reconfigure it would still
    be open against the previous file, so every subsequent query would read
    and write the wrong database while appearing to work.
    """
    global ROOT, DBPATH, LOGFILE
    old = getattr(_local, "conn", None)
    if old is not None:
        try:
            old.close()
        except sqlite3.Error:
            pass
        _local.conn = None
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


def private(path):
    """Owner-only, best effort.

    The directory is already 0700, which is what actually contains these
    files on this host. The modes matter anyway: a database copied out by a
    backup script, an rsync that preserves permissions, or a restore into a
    less careful directory all carry the file's own mode with them, and
    0644 on a file holding mail and bank records is the wrong default to
    hand to any of them.
    """
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def log(message):
    line = "%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    with _log_lock:
        try:
            existed = os.path.exists(LOGFILE)
            with open(LOGFILE, "a") as fh:
                fh.write(line)
            if not existed:
                private(LOGFILE)
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
    # WAL and the shared-memory index are created beside the database and
    # carry the same contents, so they get the same treatment.
    target = path or DBPATH
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(target + suffix):
            private(target + suffix)
    return conn


def get_conn():
    """One connection per thread. sqlite3 objects are not thread-safe."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _local.conn = connect()
    return conn
