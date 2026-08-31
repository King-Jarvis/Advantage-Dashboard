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

from . import auth, auth_google, security, statements, stats, storage

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
    ("config",   {"GET"},         re.compile(r"^/api/config$"),            "none"),
    ("g_start",  {"GET"},         re.compile(r"^/api/auth/google/start$"),    "none"),
    ("g_cb",     {"GET"},         re.compile(r"^/api/auth/google/callback$"), "none"),
    ("accounts", {"GET", "POST"}, re.compile(r"^/api/accounts$"),          "session"),
    ("budget",   {"GET"},         re.compile(r"^/api/view/budget$"),       "session"),
    ("suggest",  {"GET"},         re.compile(r"^/api/view/suggestions$"),  "session"),
    ("overview", {"GET"},         re.compile(r"^/api/view/overview$"),     "session"),
    ("history",  {"GET"},         re.compile(r"^/api/view/history$"),      "session"),
    ("coverage", {"GET"},         re.compile(r"^/api/view/coverage$"),     "session"),
    ("setbudget", {"PATCH"},
     re.compile(r"^/api/edit/budget/(\d{4}-\d{2})/([0-9a-f]{32})$"), "session"),
    ("movemoney", {"POST"},
     re.compile(r"^/api/edit/budget/(\d{4}-\d{2})/move$"), "session"),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "Dashboard"
    protocol_version = "HTTP/1.1"

    @property
    def secure(self):
        """Is the browser on HTTPS?

        Derived from the server rather than set as a flag, so it cannot drift
        from reality. Getting this wrong is quiet in both directions: too low
        and HSTS is skipped, too high and the Secure cookie is dropped by the
        browser so login silently does nothing.
        """
        return bool(self.server.tls_context) or self.server.behind_proxy_tls

    # ── plumbing ──────────────────────────────────────────────────────────
    def log_message(self, fmt, *args):
        if os.environ.get("DASHBOARD_VERBOSE"):
            storage.log("http " + (fmt % args))

    def log_error(self, fmt, *args):
        storage.log("http error: " + (fmt % args))

    def handle_one_request(self):
        # One handler instance serves every request on a keep-alive
        # connection, so per-request state must be reset here. Leaving _sent
        # True from the previous response makes the next request look already
        # answered, and nothing is written -- the browser then waits forever
        # on a connection that will never speak again.
        self._sent = False
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

    def api_config(self, conn, session):
        """What the sign-in page needs to know before anyone is signed in.

        Deliberately says only whether Google sign-in is *available* -- never
        the client id, and never anything about which accounts exist.
        """
        self.json_out({"google_enabled": auth_google.configured()})

    def _redirect(self, location, extra=None):
        headers = {"Location": location}
        headers.update(extra or {})
        self._send(303, b"", "text/plain", headers)

    def api_g_start(self, conn, session):
        try:
            url = auth_google.begin(
                conn, purpose="signin",
                return_to=(self.query().get("return_to") or ["/"])[0])
        except auth_google.OAuthError as e:
            return self.fail(503, str(e))
        self._redirect(url)

    def api_g_cb(self, conn, session):
        q = self.query()
        if q.get("error"):
            # The user declined, or Google refused. Neither is an error worth
            # a stack trace; send them back to a page that makes sense.
            return self._redirect("/?auth=denied")

        state = (q.get("state") or [""])[0]
        code = (q.get("code") or [""])[0]
        pending = auth_google.take_pending(conn, state)
        if pending is None or not code:
            # Unknown, replayed or expired state. Say nothing specific.
            return self._redirect("/?auth=failed")

        try:
            tokens = auth_google.exchange(code, pending["code_verifier"])
            info = auth_google.identity(tokens["access_token"])
        except auth_google.OAuthError:
            storage.log("google callback: exchange or userinfo failed")
            return self._redirect("/?auth=failed")

        sub = info["sub"]
        allowed = auth_google.allowed_subs()
        if sub not in allowed:
            # Logged so the operator can copy the id into ALLOWED_GOOGLE_SUBS.
            # The email is deliberately not logged.
            storage.log("google sign-in refused for sub=%s (not in "
                        "ALLOWED_GOOGLE_SUBS)" % sub)
            return self._redirect("/?auth=notallowed")

        row = conn.execute("SELECT id FROM users WHERE google_sub=?",
                           (sub,)).fetchone()
        if row is None:
            username = info.get("email") or ("google:" + sub)
            existing = conn.execute("SELECT id FROM users WHERE username=?",
                                    (username,)).fetchone()
            if existing:
                conn.execute("UPDATE users SET google_sub=? WHERE id=?",
                             (sub, existing["id"]))
                conn.commit()
                user_id = existing["id"]
            else:
                user_id = auth.create_user(conn, username, google_sub=sub)
        else:
            user_id = row["id"]

        sid, _csrf = auth.create_session(
            conn, user_id, ip=self.client_address[0],
            user_agent=self.headers.get("User-Agent", ""))
        self._redirect(auth_google.safe_return_to(pending["return_to"]), {
            "Set-Cookie": security.cookie(
                SESSION_COOKIE, sid, secure=self.secure,
                max_age=auth.SESSION_ABSOLUTE_DAYS * 86400)})

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

    def _month(self):
        month = (self.query().get("month") or [""])[0]
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValueError("month must be YYYY-MM")
        return month

    def api_budget(self, conn, session):
        from . import ledger
        month = self._month()
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

    def api_overview(self, conn, session):
        """Everything the main screen draws, in one request.

        The overview is the first thing seen after signing in, so it is a
        single round trip rather than a budget call followed by a slower
        suggestions call. Categories with nothing budgeted and nothing spent
        are dropped: an empty envelope is not a shape worth drawing.
        """
        from . import ledger
        month = self._month()
        analyses = {a["category_id"]: a
                    for a in stats.analyse_all(conn, end_month=month)}
        cats = conn.execute(
            "SELECT c.id, c.name, g.name gname FROM categories c"
            " JOIN category_groups g ON g.id = c.group_id"
            " WHERE c.is_income=0 AND c.hidden=0 ORDER BY g.sort, c.sort"
        ).fetchall()

        items, short = [], 0
        for c in cats:
            a = analyses.get(c["id"], {})
            budgeted = ledger.get_budget(conn, month, c["id"])
            # What history says this will actually cost, as distinct from
            # what it recommends you budget -- for a sinking fund those
            # differ, and the chart should not pretend otherwise.
            estimate = a.get("trimmed_mean_cents") or 0
            recommended = a.get("suggested_cents")
            if not budgeted and not estimate and not recommended:
                continue
            if budgeted < estimate:
                short += 1
            items.append({
                "id": c["id"], "name": c["name"], "group": c["gname"],
                "budgeted": budgeted,
                "recommended": recommended,
                "estimate": estimate,
                "activity": ledger.category_activity(conn, c["id"], month),
                "balance": ledger.category_balance(conn, c["id"], month),
                "kind": a.get("kind", ""),
                "confidence": a.get("confidence", "none"),
                "sample_months": a.get("sample_months", 0),
            })

        self.json_out({
            "month": month,
            "to_be_budgeted_cents": ledger.to_be_budgeted(conn, month),
            "budgeted_total_cents": sum(i["budgeted"] for i in items),
            "estimate_total_cents": sum(i["estimate"] for i in items),
            "recommended_total_cents": sum(i["recommended"] or 0 for i in items),
            "short_categories": short,
            "items": items,
        })

    def api_suggest(self, conn, session):
        """What the history says each category costs.

        Deliberately a separate call from the budget view: it is slower, and
        the budget must render immediately whether or not suggestions are
        available.
        """
        month = self._month()
        rows = stats.analyse_all(conn, end_month=month)
        self.json_out({
            "month": month,
            "totals": stats.totals(conn, end_month=month),
            "suggestions": [{
                "category_id": r["category_id"], "name": r["name"],
                "group": r["group"], "kind": r["kind"],
                "confidence": r["confidence"],
                "sample_months": r["sample_months"],
                "suggested_cents": r["suggested_cents"],
                "low_cents": r["low_cents"], "high_cents": r["high_cents"],
                "trend_pct": r["trend_pct"], "basis": r["basis"],
                "occurrences": r.get("occurrences", 0),
            } for r in rows]})

    def api_history(self, conn, session):
        """Monthly spend for one category, for the chart."""
        from . import ledger
        cat = (self.query().get("category") or [""])[0]
        if not re.fullmatch(r"[0-9a-f]{32}", cat):
            raise ValueError("category must be an id")
        try:
            window = int((self.query().get("months") or ["12"])[0])
        except ValueError:
            raise ValueError("months must be a number") from None
        window = max(1, min(36, window))
        month = self._month()
        a = stats.analyse(conn, cat, end_month=month, window=window)
        self.json_out({
            "category_id": cat, "months": a["months"],
            "spend": [a["spend_by_month"][m] for m in a["months"]],
            "budgeted": [ledger.get_budget(conn, m, cat) for m in a["months"]],
            "suggested_cents": a["suggested_cents"],
            "low_cents": a["low_cents"], "high_cents": a["high_cents"],
            "kind": a["kind"], "confidence": a["confidence"],
            "sample_months": a["sample_months"], "basis": a["basis"],
        })

    def api_coverage(self, conn, session):
        """Which months have statements behind them, and which do not."""
        self.json_out({"covered": statements.coverage(conn),
                       "gaps": statements.gaps(conn)})

    def api_setbudget(self, conn, session, month, category_id):
        from . import ledger
        data = self.body_json()
        cents = data.get("budgeted_cents")
        if not isinstance(cents, int):
            raise ValueError("budgeted_cents must be an integer number of cents")
        ledger.set_budget(conn, month, category_id, cents)
        self.json_out({
            "month": month, "category_id": category_id,
            "budgeted_cents": cents,
            "balance_cents": ledger.category_balance(conn, category_id, month),
            "to_be_budgeted_cents": ledger.to_be_budgeted(conn, month)})

    def api_movemoney(self, conn, session, month):
        from . import ledger
        data = self.body_json()
        src, dst = data.get("from_category"), data.get("to_category")
        cents = data.get("cents")
        if not isinstance(cents, int) or cents <= 0:
            raise ValueError("cents must be a positive integer")
        for cid in (src, dst):
            if not isinstance(cid, str) or not re.fullmatch(r"[0-9a-f]{32}", cid):
                raise ValueError("category ids are required")
        ledger.move_money(conn, month, src, dst, cents)
        self.json_out({
            "month": month,
            "from": {"id": src,
                     "budgeted_cents": ledger.get_budget(conn, month, src),
                     "balance_cents": ledger.category_balance(conn, src, month)},
            "to": {"id": dst,
                   "budgeted_cents": ledger.get_budget(conn, month, dst),
                   "balance_cents": ledger.category_balance(conn, dst, month)},
            "to_be_budgeted_cents": ledger.to_be_budgeted(conn, month)})

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


class DashboardServer(ThreadingHTTPServer):
    """Threading server, optionally with TLS.

    The TLS handshake happens in the worker thread, not on the accept loop.
    Wrapping the *listening* socket is the obvious approach and it is wrong:
    accept() then performs the handshake inline, so a client that opens a TCP
    connection without immediately sending a ClientHello blocks every other
    connection. Browsers do exactly that -- Chrome preconnects sockets
    speculatively -- so the symptom is that curl works perfectly and the site
    never loads in a browser.
    """

    daemon_threads = True
    # Python's default of 5 is small for a browser opening six connections at
    # once plus preconnects.
    request_queue_size = 64

    # Set by bind(). Together these decide whether the browser is on HTTPS.
    tls_context = None
    behind_proxy_tls = False

    def get_request(self):
        sock, addr = self.socket.accept()
        # A stalled handshake must not hold a worker thread forever.
        sock.settimeout(30)
        return sock, addr

    def finish_request(self, request, client_address):
        if self.tls_context is not None:
            try:
                request = self.tls_context.wrap_socket(request, server_side=True)
            except (OSError, ValueError):
                # Plain HTTP sent to a TLS port, an abandoned preconnect, or a
                # client that gave up. None of these is worth a traceback.
                return
        self.RequestHandlerClass(request, client_address, self)


def bind(host, preferred, tls_context=None, behind_proxy_tls=False):
    """Bind `preferred`, falling back to an ephemeral port if it is taken.

    Always reports the port actually bound, never the one asked for: passing
    0 means "any free port", and returning the request would report 0.
    """
    try:
        httpd = DashboardServer((host, preferred), Handler)
    except OSError:
        httpd = DashboardServer((host, 0), Handler)
    httpd.tls_context = tls_context
    httpd.behind_proxy_tls = behind_proxy_tls
    return httpd, httpd.server_address[1]
