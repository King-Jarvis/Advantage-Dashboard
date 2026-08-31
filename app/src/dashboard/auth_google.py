"""Sign in with Google, and connecting Google accounts.

Authorization Code flow with PKCE, using only the standard library.

Two things here are deliberate and worth knowing before changing them.

**No local ID-token verification.** Verifying a Google ID token means checking
an RS256 signature, and hashlib has no RSA. Rather than hand-roll that or add
a dependency, the access token is presented to Google's userinfo endpoint and
the identity in the response is trusted. That costs one HTTPS round trip per
sign-in and is sound: the token came from Google over TLS and is being handed
straight back to Google.

**Sign-in and data access are separate grants.** Signing in asks only for
identity. The scopes that read and modify mail and calendar are requested
later, by an explicit Connect action, so nobody is asked to hand over their
inbox merely to look at a login page.
"""

import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

SIGNIN_SCOPES = ["openid", "email", "profile"]
CONNECT_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.events",
]

PENDING_TTL_MINUTES = 10
HTTP_TIMEOUT = 15


class OAuthError(Exception):
    pass


# ── configuration ─────────────────────────────────────────────────────────
def client_id():
    return os.environ.get("GOOGLE_CLIENT_ID", "")


def client_secret():
    """Read from a file, never an environment value.

    An env var is visible to anything that can run `docker inspect` or read
    /proc/<pid>/environ; a file mounted at /run/secrets is not.
    """
    path = os.environ.get("GOOGLE_CLIENT_SECRET_PATH", "")
    if path and os.path.exists(path):
        with open(path) as fh:
            return fh.read().strip()
    return os.environ.get("GOOGLE_CLIENT_SECRET", "")


def redirect_uri():
    base = os.environ.get("BASE_URL", "http://localhost:8766").rstrip("/")
    return base + "/api/auth/google/callback"


def configured():
    return bool(client_id() and client_secret())


def allowed_subs():
    """Google account ids permitted to sign in.

    Empty means nobody. A fresh deployment of a public project must not be
    open to any Google account on the internet, so the default is closed and
    the operator opts a specific account in.
    """
    raw = os.environ.get("ALLOWED_GOOGLE_SUBS", "")
    return {s.strip() for s in raw.split(",") if s.strip()}


# ── helpers ───────────────────────────────────────────────────────────────
def _now():
    return datetime.now(UTC).replace(microsecond=0)


def _b64(raw):
    return urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def safe_return_to(value):
    """Only same-origin relative paths.

    Without this, `?return_to=https://evil.example` turns the callback into
    an open redirect wearing this site's name.
    """
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    if "\\" in value or "\n" in value or "\r" in value:
        return "/"
    return value


def _post_form(url, fields):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        # Google's error body can echo request detail; never surface it.
        raise OAuthError("token exchange failed (%s)" % e.code) from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise OAuthError("could not reach Google: %s" % e.reason) from None


def _get_json(url, access_token):
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + access_token,
                      "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise OAuthError("userinfo failed (%s)" % e.code) from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise OAuthError("could not reach Google: %s" % e.reason) from None


# ── flow ──────────────────────────────────────────────────────────────────
def begin(conn, purpose="signin", return_to="/"):
    """Create a pending authorisation and return the URL to send the user to."""
    if not configured():
        raise OAuthError("Google sign-in is not configured")
    if purpose not in ("signin", "connect"):
        raise OAuthError("unknown purpose")

    verifier = _b64(secrets.token_bytes(48))
    challenge = _b64(sha256(verifier.encode("ascii")).digest())
    state = secrets.token_urlsafe(32)
    now = _now()

    conn.execute(
        "INSERT INTO oauth_pending (state, code_verifier, purpose, return_to,"
        " created_at, expires_at) VALUES (?,?,?,?,?,?)",
        (state, verifier, purpose, safe_return_to(return_to),
         now.isoformat(),
         (now + timedelta(minutes=PENDING_TTL_MINUTES)).isoformat()))
    conn.commit()

    scopes = SIGNIN_SCOPES if purpose == "signin" else SIGNIN_SCOPES + CONNECT_SCOPES
    params = {
        "client_id": client_id(),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # offline + consent so a refresh token is actually issued; Google
        # returns one only on the first consent unless prompted again.
        "access_type": "offline",
        "prompt": "consent" if purpose == "connect" else "select_account",
        "include_granted_scopes": "true",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def take_pending(conn, state):
    """Consume a pending authorisation. Single use, and expired rows are dead."""
    row = conn.execute("SELECT * FROM oauth_pending WHERE state=?",
                       (state,)).fetchone()
    if row is None:
        return None
    # Delete before use, so a replayed callback finds nothing regardless of
    # what happens next.
    conn.execute("DELETE FROM oauth_pending WHERE state=?", (state,))
    conn.commit()
    if datetime.fromisoformat(row["expires_at"]) <= _now():
        return None
    return row


def exchange(code, verifier):
    """Swap the authorisation code for tokens."""
    data = _post_form(TOKEN_URL, {
        "code": code,
        "client_id": client_id(),
        "client_secret": client_secret(),
        "redirect_uri": redirect_uri(),
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    })
    if "access_token" not in data:
        raise OAuthError("no access token in response")
    return data


def identity(access_token):
    """Who Google says this is. `sub` is the stable id; email can change."""
    info = _get_json(USERINFO_URL, access_token)
    if not info.get("sub"):
        raise OAuthError("no subject in userinfo")
    return info


def refresh(refresh_token):
    return _post_form(TOKEN_URL, {
        "refresh_token": refresh_token,
        "client_id": client_id(),
        "client_secret": client_secret(),
        "grant_type": "refresh_token",
    })


def purge_expired(conn):
    n = conn.execute("DELETE FROM oauth_pending WHERE expires_at <= ?",
                     (_now().isoformat(),)).rowcount
    conn.commit()
    return n
