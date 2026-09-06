"""Claiming a fresh installation.

A dashboard with no users has to let someone become the first one, and that
moment is the weakest point in the whole system: the account created here can
read the mail, the calendar and the finances that follow.

The installation is meant to be reachable from every device on the house
network, so "first person to load the page wins" is not acceptable -- that is
not a hypothetical, it is a guest on the wifi. Instead the server writes a
claim token to a file only its own user can read, and the installer prints it.
Proving you can read a file on the machine is a reasonable stand-in for
proving you own the machine.

The token exists only while it is needed: it is written when the database has
no users and deleted the moment one exists. There is no window in which a
stale token still opens anything.
"""
import os
import threading

from . import auth, security, storage

TOKEN_FILE = "setup-token"
TOKEN_BYTES = 32
MIN_PASSWORD = auth.MIN_PASSWORD

# Two browsers submitting the form at the same moment must not both succeed.
_claim_lock = threading.Lock()


class SetupError(Exception):
    pass


def token_path():
    return os.path.join(storage.ROOT, TOKEN_FILE)


def needs_setup(conn):
    return conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0


def ensure_token(conn):
    """Return the claim token, creating it if this install has no users.

    Returns None once a user exists, and removes any leftover file at the same
    time, so the token cannot outlive its purpose.
    """
    path = token_path()
    if not needs_setup(conn):
        clear_token()
        return None
    if os.path.exists(path):
        try:
            with open(path) as fh:
                existing = fh.read().strip()
            if existing:
                return existing
        except OSError:
            pass
    token = security.new_token(TOKEN_BYTES)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token + "\n")
    return token


def clear_token():
    try:
        os.remove(token_path())
    except OSError:
        pass


def claim(conn, token, username, password, ip="", user_agent=""):
    """Create the first user and open a session. Returns (sid, csrf).

    Every failure raises SetupError with a message meant to be shown; none of
    them reveal anything a stranger could not already guess by loading the
    page.
    """
    username = str(username or "").strip()
    password = str(password or "")

    # Order matters. An install that is already claimed must say so before it
    # complains about a password, or someone finding this page has to guess
    # their way past three field errors to learn the one thing that actually
    # explains what they are seeing.
    with _claim_lock:
        if not needs_setup(conn):
            # Someone got here first. Say so plainly: the honest reading is
            # that the install is already claimed, and if that was not you,
            # you have a real problem worth knowing about immediately.
            raise SetupError("this dashboard has already been set up -- "
                             "sign in instead")
        expected = ensure_token(conn)
        if not expected or not security.csrf_ok(expected, str(token or "")):
            raise SetupError("that setup code is not right")
        # Field validation last, so a typo here costs a retype and not the
        # code -- the token is only spent on success.
        if not username:
            raise SetupError("choose a username")
        if len(password) < MIN_PASSWORD:
            raise SetupError("use a password of at least %d characters -- "
                             "this one account protects your mail, calendar "
                             "and finances" % MIN_PASSWORD)
        uid = auth.create_user(conn, username, password=password)
        clear_token()
    storage.log("first user created: %s" % username)
    return auth.create_session(conn, uid, ip=ip, user_agent=user_agent)
