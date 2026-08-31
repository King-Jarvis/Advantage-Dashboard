"""HTTP layer: routing, sessions, static files.

Structure follows the moodboards server that already runs on this network --
a ROUTES table of compiled patterns, a dispatch method, and no framework. Its
rendering approach is deliberately NOT carried over: that app assigns
innerHTML in twenty-one places, which is fine for images you chose yourself
and a stored-XSS vector for email subjects and payees.
"""

import json
import mimetypes
import os
import re
import traceback
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import auth, security, storage

PACKAGE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(PACKAGE, "static")

SESSION_COOKIE = "dash_session"
CSRF_HEADER = "X-CSRF-Token"

# Bodies are small JSON documents. A cap stops a malformed or hostile request
# from being read into memory unbounded.
MAX_BODY = 2 * 1024 * 1024

# (name, method-set, pattern, auth)
#   auth: "none"    reachable unauthenticated
#         "session" browser session; mutations additionally need CSRF
#         "ingest"  the n8n key only -- never reaches view or edit routes
ROUTES = [
    ("login",    {"POST"},        re.compile(r"^/api/auth/login$"),        "none"),
    ("logout",   {"POST"},        re.compile(r"^/api/auth/logout$"),       "session"),
    ("whoami",   {"GET"},         re.compile(r"^/api/auth/whoami$"),       "session"),
    ("health",   {"GET"},         re.compile(r"^/api/health$"),            "none"),
    ("accounts", {"GET", "POST"}, re.compile(r"^/api/accounts$"),          "session"),
    ("budget",   {"GET"},         re.compile(r"^/api/view/budget$"),       "session"),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "Dashboard"
    protocol_version = "HTTP/1.1"
    secure = False           # set True when served over TLS, for HSTS

    # ── plumbing ──────────────────────────────────────────────────────────
    def log_message(self, fmt, *args):
        if os.environ.get("DASHBOARD_VERBOSE"):
            storage.log("http " + (fmt % args))

    def log_error(self, fmt, *args):
        storage.log("http error: " + (fmt % args))

    def handle_one_request(self):
        # A handler thread that dies takes its connection with it, which the
        # browser experiences as the page hanging. Log it instead.
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            storage.log("handler crashed:\n" + traceback.format_exc())
            self.close_connection = True

    def _send(self, code, body=b"", ctype="application/octet-stream", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in security.response_headers(self.secure).items():
            self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def json_out(self, payload, code=200, extra=None):
        self._send(code, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    def fail(self, code, message):
        # A bare code and a short string: error text is echoed back to the
        # caller, so it must never carry internal detail.
        self.json_out({"error": message}, code)

    def body_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("body too large")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("body is not valid JSON") from None

    def query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    # ── auth ──────────────────────────────────────────────────────────────
    def _cookie(self, name):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            return SimpleCookie(raw).get(name).value
        except (AttributeError, KeyError):
            return None

    def _load_session(self, conn):
        return auth.get_session(conn, self._cookie(SESSION_COOKIE))

    def _authorised(self, conn, need):
        """Return (ok, session). Sets its own error response when not ok."""
        if need == "none":
            return True, None

        if need == "ingest":
            supplied = self.headers.get("X-Ingest-Key")
            expected = os.environ.get("INGEST_KEY", "")
            # An ingest key must never open a browser route, and a session
            # must never open an ingest route: compromising one grants
            # nothing of the other.
            if not (expected and security.csrf_ok(expected, supplied)):
                self.fail(401, "unauthorised")
                return False, None
            return True, None

        session = self._load_session(conn)
        if session is None:
            self.fail(401, "unauthorised")
            return False, None

        if self.command not in ("GET", "HEAD"):
            if not security.csrf_ok(session["csrf_token"],
                                    self.headers.get(CSRF_HEADER)):
                # SameSite=Strict should already have stopped this. The token
                # is the second lock, for browsers or configurations where it
                # did not.
                self.fail(403, "csrf token missing or invalid")
                return False, None
        return True, session

    # ── dispatch ──────────────────────────────────────────────────────────
    def _dispatch(self):
        path = urllib.parse.urlparse(self.path).path
        for name, methods, pattern, need in ROUTES:
            m = pattern.match(path)
            if not m:
                continue
            if self.command not in methods:
                return self.fail(405, "method not allowed")
            conn = storage.get_conn()
            ok, session = self._authorised(conn, need)
            if not ok:
                return None
            try:
                return getattr(self, "api_" + name)(conn, session, *m.groups())
            except ValueError as e:
                return self.fail(400, str(e))
            except PermissionError:
                return self.fail(403, "forbidden")
        return None

    def do_GET(self):
        if self._dispatch() is None and not self._responded():
            self.serve_static()

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if self._dispatch() is None and not self._responded():
            self.fail(404, "not found")

    do_PATCH = do_PUT = do_DELETE = do_POST

    def _responded(self):
        return getattr(self, "_sent", False)

    def send_response(self, code, message=None):
        self._sent = True
        BaseHTTPRequestHandler.send_response(self, code, message)

    # ── endpoints ─────────────────────────────────────────────────────────
    def api_health(self, conn, session):
        self.json_out({"status": "ok"})

    def api_login(self, conn, session):
        data = self.body_json()
        result = auth.login(conn, str(data.get("username", "")),
                            str(data.get("password", "")),
                            ip=self.client_address[0],
                            user_agent=self.headers.get("User-Agent", ""))
        if result is None:
            # One message for every failure mode. Distinguishing "no such
            # user" from "wrong password" hands over half the credential.
            return self.fail(401, "invalid username or password")
        sid, csrf = result
        self.json_out(
            {"ok": True, "csrf_token": csrf},
            extra={"Set-Cookie": security.cookie(
                SESSION_COOKIE, sid, secure=self.secure,
                max_age=auth.SESSION_ABSOLUTE_DAYS * 86400)})

    def api_logout(self, conn, session):
        auth.destroy_session(conn, session["id"])
        self.json_out({"ok": True}, extra={
            "Set-Cookie": security.expire_cookie(SESSION_COOKIE,
                                                 secure=self.secure)})

    def api_whoami(self, conn, session):
        user = conn.execute("SELECT username FROM users WHERE id=?",
                            (session["user_id"],)).fetchone()
        self.json_out({"username": user["username"],
                       "csrf_token": session["csrf_token"]})

    def api_accounts(self, conn, session):
        from . import ledger
        if self.command == "GET":
            rows = conn.execute(
                "SELECT id, name, type, on_budget, closed FROM accounts"
                " ORDER BY name").fetchall()
            return self.json_out({"accounts": [
                dict(r) | {"balance_cents": ledger.account_balance(conn, r["id"])}
                for r in rows]})
        data = self.body_json()
        name = str(data.get("name", "")).strip()
        if not name:
            raise ValueError("name is required")
        aid = ledger.create_account(conn, name,
                                    type=str(data.get("type", "checking")),
                                    on_budget=bool(data.get("on_budget", True)))
        self.json_out({"id": aid}, 201)

    def api_budget(self, conn, session):
        from . import ledger
        month = (self.query().get("month") or [""])[0]
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValueError("month must be YYYY-MM")
        cats = conn.execute(
            "SELECT c.id, c.name, g.name gname FROM categories c"
            " JOIN category_groups g ON g.id = c.group_id"
            " WHERE c.is_income=0 AND c.hidden=0 ORDER BY g.sort, c.sort"
        ).fetchall()
        self.json_out({
            "month": month,
            "to_be_budgeted_cents": ledger.to_be_budgeted(conn, month),
            "categories": [{
                "id": c["id"], "name": c["name"], "group": c["gname"],
                "budgeted_cents": ledger.get_budget(conn, month, c["id"]),
                "activity_cents": ledger.category_activity(conn, c["id"], month),
                "balance_cents": ledger.category_balance(conn, c["id"], month),
            } for c in cats]})

    # ── static ────────────────────────────────────────────────────────────
    def serve_static(self):
        path = urllib.parse.urlparse(self.path).path
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = os.path.realpath(os.path.join(STATIC, rel))
        base = os.path.realpath(STATIC)
        # Containment check against the resolved path: without realpath,
        # a symlink inside STATIC is a way out of it.
        if not (target == base or target.startswith(base + os.sep)):
            return self.fail(404, "not found")
        if not os.path.isfile(target):
            return self.fail(404, "not found")
        ctype, _ = mimetypes.guess_type(target)
        with open(target, "rb") as fh:
            self._send(200, fh.read(), ctype or "application/octet-stream")


def bind(host, preferred):
    """Bind `preferred`, falling back to an ephemeral port if it is taken.

    Always reports the port actually bound, never the one asked for: passing
    0 means "any free port", and returning the request would report 0.
    """
    try:
        httpd = ThreadingHTTPServer((host, preferred), Handler)
    except OSError:
        httpd = ThreadingHTTPServer((host, 0), Handler)
    return httpd, httpd.server_address[1]
