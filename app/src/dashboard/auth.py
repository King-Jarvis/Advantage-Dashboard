"""Users, passwords and sessions.

Password hashing is scrypt from hashlib -- memory-hard, in the standard
library, and appropriate here. The alternative worth having is Argon2id, but
that is a dependency, and this project spends its one dependency on
encrypting OAuth tokens at rest, which protects something scrypt cannot.

There is no signup route. The first account is created by bootstrap; further
accounts are a deliberate act, not a form on the internet.
"""

import hashlib
import hmac
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta

# ~16 MB and roughly 50-100 ms on modest hardware. High enough to make
# offline guessing expensive, low enough that a Pi does not stall on login.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 64 * 1024 * 1024
DKLEN = 32

SESSION_IDLE_HOURS = 24
SESSION_ABSOLUTE_DAYS = 7

MAX_FAILURES = 5
LOCKOUT_SECONDS = 300

# The floor for any password this application sets, wherever it is set from.
# Kept here rather than at each call site so the browser setup form and the
# command line cannot drift into disagreeing about what is acceptable.
MIN_PASSWORD = 12


def _now():
    return datetime.now(UTC).replace(microsecond=0)


def _iso(dt):
    return dt.isoformat()


def _parse(s):
    return datetime.fromisoformat(s) if s else None


# ── passwords ─────────────────────────────────────────────────────────────
def hash_password(password, salt=None):
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N,
                        r=SCRYPT_R, p=SCRYPT_P, maxmem=SCRYPT_MAXMEM,
                        dklen=DKLEN)
    return dk.hex(), salt.hex()


def verify_password(password, pw_hash, pw_salt):
    if not (password and pw_hash and pw_salt):
        return False
    try:
        candidate, _ = hash_password(password, salt=bytes.fromhex(pw_salt))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, pw_hash)


# ── users ─────────────────────────────────────────────────────────────────
def create_user(conn, username, password=None, google_sub=None):
    if not username:
        raise ValueError("username is required")
    if not password and not google_sub:
        raise ValueError("a user needs either a password or a Google account")
    pw_hash = pw_salt = None
    if password:
        pw_hash, pw_salt = hash_password(password)
    uid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO users (id, username, pw_hash, pw_salt, google_sub, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (uid, username, pw_hash, pw_salt, google_sub, _iso(_now())))
    conn.commit()
    return uid


def set_password(conn, username, password):
    """Replace a user's password. Returns True, or False if no such user.

    Every session belonging to that user is destroyed. A password is reset
    either because it was forgotten or because it was exposed, and in the
    second case leaving the old sessions alive would mean the reset changed
    nothing for whoever was already inside. Being signed out of your other
    devices is the correct and expected cost.

    The lockout is cleared too: locking someone out of an account they have
    just proved they control, using a counter from before the reset, would be
    punishing the wrong person.
    """
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise ValueError("password must be at least %d characters"
                         % MIN_PASSWORD)
    row = get_user(conn, username)
    if row is None:
        return False
    pw_hash, pw_salt = hash_password(password)
    conn.execute("UPDATE users SET pw_hash=?, pw_salt=?, failed_count=0,"
                 " locked_until=NULL WHERE id=?",
                 (pw_hash, pw_salt, row["id"]))
    destroy_user_sessions(conn, row["id"])
    conn.commit()
    return True


def get_user(conn, username):
    return conn.execute("SELECT * FROM users WHERE username=?",
                        (username,)).fetchone()


def is_locked(row):
    until = _parse(row["locked_until"]) if row["locked_until"] else None
    return bool(until and until > _now())


def authenticate(conn, username, password):
    """Return the user row, or None.

    Deliberately gives the caller no way to tell an unknown username from a
    wrong password: both return None, and an unknown username still pays the
    cost of a hash so the response time does not reveal which it was.
    """
    row = get_user(conn, username)
    if row is None:
        # Burn comparable time. Without this, a fast rejection tells an
        # attacker the username does not exist, which is half the credential.
        hash_password(password or "x")
        return None

    if is_locked(row):
        return None

    if not verify_password(password, row["pw_hash"], row["pw_salt"]):
        failures = row["failed_count"] + 1
        locked = (_iso(_now() + timedelta(seconds=LOCKOUT_SECONDS))
                  if failures >= MAX_FAILURES else None)
        conn.execute("UPDATE users SET failed_count=?, locked_until=? WHERE id=?",
                     (failures, locked, row["id"]))
        conn.commit()
        return None

    conn.execute("UPDATE users SET failed_count=0, locked_until=NULL,"
                 " last_login_at=? WHERE id=?", (_iso(_now()), row["id"]))
    conn.commit()
    return get_user(conn, username)


# ── sessions ──────────────────────────────────────────────────────────────
def create_session(conn, user_id, ip="", user_agent=""):
    """A new session id and CSRF token.

    The id is rotated on every login rather than reused, so a session id
    observed before authentication cannot become an authenticated one.
    """
    now = _now()
    sid = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (id, user_id, csrf_token, created_at,"
        " last_seen_at, expires_at, ip, user_agent) VALUES (?,?,?,?,?,?,?,?)",
        (sid, user_id, csrf, _iso(now), _iso(now),
         _iso(now + timedelta(days=SESSION_ABSOLUTE_DAYS)), ip,
         (user_agent or "")[:200]))
    conn.commit()
    return sid, csrf


def get_session(conn, sid, touch=True):
    """Return a live session, or None.

    Two expiries. The absolute one is set at creation and never extended, so
    a stolen session cannot be kept alive indefinitely by using it. The idle
    one closes sessions nobody is using.
    """
    if not sid:
        return None
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if row is None:
        return None

    now = _now()
    if _parse(row["expires_at"]) <= now:
        destroy_session(conn, sid)
        return None
    if _parse(row["last_seen_at"]) + timedelta(hours=SESSION_IDLE_HOURS) <= now:
        destroy_session(conn, sid)
        return None

    if touch:
        conn.execute("UPDATE sessions SET last_seen_at=? WHERE id=?",
                     (_iso(now), sid))
        conn.commit()
    return row


def destroy_session(conn, sid):
    conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
    conn.commit()


def destroy_user_sessions(conn, user_id):
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
    conn.commit()


def purge_expired(conn):
    n = conn.execute("DELETE FROM sessions WHERE expires_at <= ?",
                     (_iso(_now()),)).rowcount
    conn.commit()
    return n


def login(conn, username, password, ip="", user_agent=""):
    """Authenticate and open a session. Returns (sid, csrf) or None."""
    user = authenticate(conn, username, password)
    if user is None:
        # A brief, constant pause. Not a real defence on its own -- the
        # lockout is -- but it takes the edge off rapid online guessing.
        time.sleep(0.05)
        return None
    return create_session(conn, user["id"], ip=ip, user_agent=user_agent)
