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
import sqlite3
import traceback
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import (
    auth,
    auth_google,
    crypt,
    feeds,
    firstrun,
    google_api,
    imageproxy,
    push,
    savings,
    scheduler,
    security,
    settings,
    statements,
    stats,
    storage,
    sync,
    themes,
)

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
    # Claiming a fresh install is unauthenticated by necessity -- there is
    # nobody to authenticate as yet. The setup token is what stands in.
    ("claim",    {"POST"},        re.compile(r"^/api/setup/claim$"),       "none"),
    ("g_start",  {"GET"},         re.compile(r"^/api/auth/google/start$"),    "none"),
    ("g_cb",     {"GET"},         re.compile(r"^/api/auth/google/callback$"), "none"),
    ("g_check",  {"GET"},
     re.compile(r"^/api/google/check$"), "session"),
    ("g_connect", {"GET"},
     re.compile(r"^/api/google/connect$"), "session"),
    ("g_accts",  {"GET"},
     re.compile(r"^/api/google/accounts$"), "session"),
    ("g_acct",   {"DELETE"},
     re.compile(r"^/api/google/accounts/([0-9a-f]{32})$"), "session"),
    ("settings", {"GET", "PATCH"},
     re.compile(r"^/api/settings$"), "session"),
    ("setting",  {"DELETE"},
     re.compile(r"^/api/settings/([a-z_]{3,40})$"), "session"),
    ("accounts", {"GET", "POST"}, re.compile(r"^/api/accounts$"),          "session"),
    ("account",  {"PATCH"},
     re.compile(r"^/api/accounts/([0-9a-f]{32})$"),                        "session"),
    ("catall",   {"POST"},        re.compile(r"^/api/categorize$"),        "session"),
    ("budget",   {"GET"},         re.compile(r"^/api/view/budget$"),       "session"),
    ("suggest",  {"GET"},         re.compile(r"^/api/view/suggestions$"),  "session"),
    ("overview", {"GET"},         re.compile(r"^/api/view/overview$"),     "session"),
    ("home",     {"GET"},         re.compile(r"^/api/view/home$"),         "session"),
    ("agenda",   {"GET"},         re.compile(r"^/api/view/agenda$"),       "session"),
    ("calendar", {"GET"},         re.compile(r"^/api/view/calendar$"),     "session"),
    ("evnew",    {"POST"},        re.compile(r"^/api/edit/event$"),        "session"),
    ("evedit",   {"PATCH", "DELETE"},
     re.compile(r"^/api/edit/event/([0-9a-f]{32})$"),                       "session"),
    ("inbox",    {"GET"},         re.compile(r"^/api/view/inbox$"),        "session"),
    # Signed, so this can never become an open proxy on the home network.
    ("image",    {"GET"},         re.compile(r"^/api/image$"),             "session"),
    # The active theme is needed before the first paint, so it is readable
    # without a session -- it carries no personal data, only colours.
    ("themeact", {"GET"},         re.compile(r"^/api/theme$"),             "none"),
    ("themes",   {"GET", "POST"}, re.compile(r"^/api/themes$"),            "session"),
    ("theme1",   {"POST", "DELETE"},
     re.compile(r"^/api/themes/([0-9a-f]{32})$"),                          "session"),
    ("msgbody",  {"GET"},
     re.compile(r"^/api/view/message/([0-9a-f]{32})$"),                      "session"),
    ("syncst",   {"GET"},         re.compile(r"^/api/view/status$"),       "session"),
    ("editmsg",  {"PATCH"},
     re.compile(r"^/api/edit/message/([0-9a-f]{32})$"), "session"),
    ("ing_ev",   {"POST"},        re.compile(r"^/api/ingest/events$"),     "ingest"),
    ("ing_msg",  {"POST"},        re.compile(r"^/api/ingest/messages$"),   "ingest"),
    ("ing_sync", {"POST"},        re.compile(r"^/api/ingest/sync$"),       "ingest"),
    # Same work, two doors: the scheduler comes in with the ingest key,
    # the "Sync now" button comes in with a session. Neither door opens
    # the other, so a leaked ingest key still cannot read the dashboard.
    ("sync_ing", {"POST"},        re.compile(r"^/api/sync/google$"),       "ingest"),
    ("sync_ses", {"POST"},        re.compile(r"^/api/action/sync$"),       "session"),
    ("history",  {"GET"},         re.compile(r"^/api/view/history$"),      "session"),
    ("coverage", {"GET"},         re.compile(r"^/api/view/coverage$"),     "session"),
    ("txns",     {"GET"},         re.compile(r"^/api/view/transactions$"),  "session"),
    ("txnone",   {"GET"},
     re.compile(r"^/api/view/transaction/([0-9a-f]{32})$"),                 "session"),
    ("unfiled",  {"GET"},         re.compile(r"^/api/view/unfiled$"),       "session"),
    ("fileone",  {"PATCH", "DELETE"},
     re.compile(r"^/api/edit/transaction/([0-9a-f]{32})$"),                 "session"),
    ("filemany", {"POST"},        re.compile(r"^/api/edit/by-payee$"),      "session"),
    ("xfers",    {"GET"},         re.compile(r"^/api/view/transfers$"),     "session"),
    ("ledger",   {"GET"},         re.compile(r"^/api/view/ledger$"),        "session"),
    ("xferlink", {"POST", "DELETE"},
     re.compile(r"^/api/edit/transfer$"),                                  "session"),
    ("split",    {"GET", "POST", "DELETE"},
     re.compile(r"^/api/edit/transaction/([0-9a-f]{32})/split$"),           "session"),
    ("upload",   {"POST"},        re.compile(r"^/api/import/upload$"),     "session"),
    ("batches",  {"GET"},         re.compile(r"^/api/import/batches$"),    "session"),
    ("unimport", {"POST"},
     re.compile(r"^/api/import/batch/([0-9a-f]{32})/revert$"),              "session"),
    ("batch",    {"GET", "POST", "DELETE"},
     re.compile(r"^/api/import/batch/([0-9a-f]{32})$"), "session"),
    ("batchrow", {"PATCH"},
     re.compile(r"^/api/import/row/([0-9a-f]{32})$"), "session"),
    ("cats",     {"GET", "POST"}, re.compile(r"^/api/categories$"),        "session"),
    ("cat",      {"PATCH", "DELETE"},
     re.compile(r"^/api/categories/([0-9a-f]{32})$"), "session"),
    ("groups",   {"GET", "POST"}, re.compile(r"^/api/category-groups$"),   "session"),
    ("setbudget", {"PATCH"},
     re.compile(r"^/api/edit/budget/(\d{4}-\d{2})/([0-9a-f]{32})$"), "session"),
    ("movemoney", {"POST"},
     re.compile(r"^/api/edit/budget/(\d{4}-\d{2})/move$"), "session"),
    ("saveflex", {"PATCH"},
     re.compile(r"^/api/savings/flexibility/([0-9a-f]{32})$"),              "session"),
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
            # Settings first so the key can be set in the browser; the
            # environment still wins nothing but still works, for a container
            # that would rather inject it.
            expected = settings.get(conn, "ingest_key") or \
                os.environ.get("INGEST_KEY", "")
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
        self.json_out({"google_enabled": auth_google.configured(conn),
                       "needs_setup": firstrun.needs_setup(conn)})

    def api_claim(self, conn, session):
        data = self.body_json()
        try:
            sid, csrf = firstrun.claim(
                conn, data.get("token"), data.get("username"),
                data.get("password"), ip=self.client_address[0],
                user_agent=self.headers.get("User-Agent", ""))
        except firstrun.SetupError as e:
            return self.fail(400, str(e))
        self.json_out(
            {"ok": True, "csrf_token": csrf},
            extra={"Set-Cookie": security.cookie(
                SESSION_COOKIE, sid, secure=self.secure,
                max_age=auth.SESSION_ABSOLUTE_DAYS * 86400)})

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
            tokens = auth_google.exchange(code, pending["code_verifier"], conn=conn)
            info = auth_google.identity(tokens["access_token"])
        except auth_google.OAuthError:
            storage.log("google callback: exchange or userinfo failed")
            return self._redirect("/?auth=failed")

        sub = info["sub"]

        if pending["purpose"] == "connect":
            # Connecting grants access to this account's mail and calendar.
            # It is a different permission from signing in, and is only
            # offered to an already-authenticated session -- begin() refuses
            # otherwise -- so no additional allow-list check applies here.
            try:
                settings.save_google_account(
                    conn, sub=sub, email=info.get("email", ""),
                    refresh_token=tokens.get("refresh_token", ""),
                    access_token=tokens.get("access_token", ""),
                    expires_at=tokens.get("expires_at"),
                    scopes=tokens.get("scope", ""))
            except RuntimeError:
                # No encryption key: refuse rather than store a refresh token
                # in the clear.
                storage.log("google connect refused: no encryption key")
                return self._redirect("/#/settings?connect=nokey")
            return self._redirect("/#/settings?connect=ok")

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
                "SELECT id, name, type, on_budget, closed,"
                "       opening_balance_cents FROM accounts"
                " ORDER BY name").fetchall()
            return self.json_out({"accounts": [
                dict(r) | {"balance_cents": ledger.account_balance(conn, r["id"])}
                for r in rows]})
        data = self.body_json()
        name = str(data.get("name", "")).strip()
        if not name:
            raise ValueError("name is required")
        opening = data.get("opening_balance_cents", 0)
        if not isinstance(opening, int):
            raise ValueError("opening_balance_cents must be whole pence")
        aid = ledger.create_account(conn, name,
                                    type=str(data.get("type", "checking")),
                                    on_budget=bool(data.get("on_budget", True)),
                                    opening_balance_cents=opening)
        self.json_out({"id": aid}, 201)

    def api_account(self, conn, session, account_id):
        """Correct an account: its name, where it sits, where it started."""
        from . import ledger
        data = self.body_json()
        fields = {}
        if "name" in data:
            name = str(data["name"]).strip()
            if not name:
                raise ValueError("name cannot be empty")
            fields["name"] = name
        if "on_budget" in data:
            fields["on_budget"] = bool(data["on_budget"])
        if "closed" in data:
            fields["closed"] = bool(data["closed"])
        if "opening_balance_cents" in data:
            v = data["opening_balance_cents"]
            if not isinstance(v, int):
                raise ValueError("opening_balance_cents must be whole pence")
            fields["opening_balance_cents"] = v
        try:
            ledger.update_account(conn, account_id, **fields)
        except KeyError:
            return self.fail(404, "no such account")
        self.json_out({"id": account_id,
                       "balance_cents": ledger.account_balance(conn, account_id)})

    def api_catall(self, conn, session):
        """Categorise everything still uncategorised.

        Reports how each answer was reached, because the difference between
        "settled from what you already told me" and "asked a model" is the
        difference between free and not.
        """
        import os

        from . import categorize
        data = self.body_json()
        # The setting decides, not the caller. A button that spends money on
        # your behalf while the switch that governs it says off is the kind of
        # thing you only discover on a bill.
        allowed = bool(settings.get(conn, "enable_llm_categories"))
        want_model = bool(data.get("use_model", True)) and allowed
        if want_model:
            os.environ["ANTHROPIC_API_KEY"] = settings.get(conn, "anthropic_api_key")
            os.environ["CLASSIFY_MODEL"] = settings.get(conn, "classify_model")
        has_key = categorize.available()
        counts = categorize.apply_everywhere(
            conn, use_model=want_model and has_key)
        counts["model_available"] = has_key
        counts["model_allowed"] = allowed
        self.json_out(counts)

    def api_unimport(self, conn, session, batch_id):
        """Undo a committed import. Filing a statement against the wrong
        account is an ordinary mistake and should not be permanent."""
        from . import statements as st
        try:
            out = st.revert_batch(conn, batch_id)
        except KeyError:
            return self.fail(404, "no such import")
        self.json_out(out)

    def api_txnone(self, conn, session, txn_id):
        """Everything known about one charge.

        The list shows the bank's NAME, which the bank truncates. The fuller
        description it also sent has always been stored and never displayed.
        """
        from . import ledger
        try:
            self.json_out(ledger.transaction_detail(conn, txn_id))
        except KeyError:
            self.fail(404, "no such transaction")

    def api_unfiled(self, conn, session):
        from . import ledger
        filed = (self.query().get("filed") or [""])[0] == "1"
        # Off-budget accounts come too: the same dropdown offers "spent on
        # this" and "moved to there", because from a statement row those look
        # identical and the difference is the user's to state.
        tracking = [dict(r) for r in conn.execute(
            "SELECT id, name FROM accounts WHERE on_budget=0 AND closed=0"
            " ORDER BY name")]
        self.json_out({"filed": filed, "tracking": tracking,
                       "groups": ledger.payee_groups(conn, filed=filed)})

    def api_fileone(self, conn, session, txn_id):
        from . import ledger
        if self.command == "DELETE":
            # Soft-delete, and it takes whole units: half a transfer or a
            # split parent without its children is never left behind.
            n = ledger.delete_transaction(conn, txn_id)
            if not n:
                return self.fail(404, "no such transaction")
            return self.json_out({"removed": n})
        data = self.body_json()
        fields = {k: v for k, v in data.items()
                  if k in ("category_id", "payee", "notes", "date",
                           "account_id")}
        if not fields:
            raise ValueError("nothing to change")
        try:
            ledger.update_transaction(conn, txn_id, **fields)
        except KeyError:
            return self.fail(404, "no such transaction")
        self.json_out({"id": txn_id, "updated": sorted(fields)})

    def api_split(self, conn, session, txn_id):
        """Divide one payment across categories, or put it back together."""
        from . import ledger
        if self.command == "GET":
            return self.json_out({"parts": ledger.split_parts(conn, txn_id)})
        if self.command == "DELETE":
            try:
                n = ledger.unsplit_transaction(conn, txn_id)
            except KeyError:
                return self.fail(404, "no such transaction")
            return self.json_out({"removed": n})

        data = self.body_json()
        raw = data.get("parts")
        if not isinstance(raw, list) or not raw:
            raise ValueError("parts must be a non-empty list")
        parts = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("each part is an object")
            cid = str(item.get("category_id", ""))
            try:
                amount = int(item["amount_cents"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("each part needs whole-cent amount_cents") from None
            parts.append((cid, amount))
        try:
            ledger.split_transaction(conn, txn_id, parts)
        except KeyError:
            return self.fail(404, "no such transaction or category")
        self.json_out({"id": txn_id, "parts": ledger.split_parts(conn, txn_id)})

    def api_ledger(self, conn, session):
        """Everything, arranged account then group then category."""
        from . import ledger
        q = self.query()
        month = (q.get("month") or [""])[0] or None
        if month and not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValueError("month must be YYYY-MM")
        self.json_out({
            "month": month,
            "accounts": [dict(r) for r in conn.execute(
                "SELECT id, name, on_budget FROM accounts WHERE closed=0"
                " ORDER BY name")],
            "categories": [dict(r) for r in ledger.list_categories(conn)],
            "tree": ledger.ledger_tree(conn, month)})

    def api_xfers(self, conn, session):
        """Linked movements, pairs that look like one, and what will never be.

        Three lists rather than one, because "no candidates" answers a
        different question from the one being asked. Someone who knows a
        transfer is missing needs to know whether nothing matched, something
        matched and was rejected for being eleven days apart, or the other
        half is in a statement they will never import.
        """
        from . import ledger
        wide = "wide" in self.query()
        # Money to a wallet or a loan servicer has its other half in a
        # statement that will never be imported, so the scan leaves it alone
        # rather than offering coincidences as candidates.
        exclude = ledger.split_terms(settings.get(conn, "transfer_exclusions"))
        self.json_out({
            "transfers": ledger.transfers(conn),
            "candidates": ledger.transfer_candidates(conn, exclude=exclude),
            "window_days": ledger.TRANSFER_WINDOW_DAYS,
            "exclusions": exclude,
            # Only on request: each is a full sweep of the ledger, and the
            # panel opens on every visit to the import screen.
            "near_misses": ledger.near_misses(conn, exclude=exclude) if wide else [],
            "unpaired": (ledger.unpaired_movements(conn, exclude=exclude)
                         if wide else []),
            "outside": ledger.outside_movements(conn, exclude) if wide else [],
            "scanned": wide,
        })

    def api_xferlink(self, conn, session):
        from . import ledger
        data = self.body_json()
        if self.command == "DELETE":
            try:
                ids = ledger.unlink_transfer(conn, str(data.get("id", "")))
            except KeyError:
                return self.fail(404, "no such transaction")
            return self.json_out({"unlinked": ids})
        # Two shapes: link two real rows, or record where a movement went when
        # the other side was never imported.
        if data.get("account_id"):
            try:
                mirror = ledger.send_to_account(
                    conn, str(data.get("id", "")), str(data["account_id"]))
            except KeyError:
                return self.fail(404, "no such transaction or account")
            return self.json_out({"mirror_id": mirror})
        try:
            out_id, in_id = ledger.link_transfer(
                conn, str(data.get("out_id", "")), str(data.get("in_id", "")))
        except KeyError:
            return self.fail(404, "no such transaction")
        self.json_out({"out_id": out_id, "in_id": in_id})

    def api_filemany(self, conn, session):
        """File every unfiled row for one merchant.

        A merchant, not a list of ids: the client has just been shown the
        grouping and would otherwise post back a hundred ids it does not need
        to know about.
        """
        from . import ledger
        data = self.body_json()
        key = str(data.get("payee_key", ""))
        category_id = str(data.get("category_id", ""))
        if not key:
            raise ValueError("payee_key is required")
        # A destination rather than a category: these rows moved money, they
        # did not spend it.
        if data.get("account_id"):
            try:
                n = ledger.send_payee_to_account(
                    conn, key, str(data["account_id"]))
            except KeyError:
                return self.fail(404, "no such account")
            return self.json_out({"filed": n, "as": "transfer"})
        try:
            n = ledger.categorise_payee(
                conn, key, category_id,
                overwrite=bool(data.get("overwrite")))
        except KeyError:
            return self.fail(404, "no such category")
        self.json_out({"filed": n})

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
        # Income categories are not budgeted -- you budget from income, not to
        # it -- but they must still be visible. A category that vanishes the
        # moment it is marked as income looks deleted, and the money it
        # carries looks lost.
        income = conn.execute(
            "SELECT c.id, c.name, g.name gname FROM categories c"
            " JOIN category_groups g ON g.id = c.group_id"
            " WHERE c.is_income=1 AND c.hidden=0 ORDER BY g.sort, c.sort"
        ).fetchall()
        self.json_out({
            "month": month,
            "to_be_budgeted_cents": ledger.to_be_budgeted(conn, month),
            "income": [{
                "id": c["id"], "name": c["name"], "group": c["gname"],
                "activity_cents": ledger.category_activity(conn, c["id"], month),
            } for c in income],
            "categories": [{
                "id": c["id"], "name": c["name"], "group": c["gname"],
                "budgeted_cents": ledger.get_budget(conn, month, c["id"]),
                "activity_cents": ledger.category_activity(conn, c["id"], month),
                "balance_cents": ledger.category_balance(conn, c["id"], month),
            } for c in cats]})

    def _int_arg(self, name, default, lo, hi):
        try:
            v = int((self.query().get(name) or [str(default)])[0])
        except ValueError:
            raise ValueError("%s must be a number" % name) from None
        return max(lo, min(hi, v))

    def api_agenda(self, conn, session):
        days = self._int_arg("days", 7, 1, 90)
        self.json_out({"days": days,
                       "events": feeds.agenda(conn, days=days, limit=200)})

    def _event_fields(self, data):
        return {k: v for k, v in data.items() if k in feeds.EVENT_FIELDS}

    def api_evnew(self, conn, session):
        data = self.body_json()
        account = str(data.get("account_id") or "")
        if not account:
            # With one account this could be guessed, but guessing would put
            # the event in the wrong calendar the day a second one is added.
            accounts = settings.list_google_accounts(conn)
            if len(accounts) != 1:
                raise ValueError("account_id is required")
            account = accounts[0]["id"]
        try:
            eid = feeds.create_event(conn, account, **self._event_fields(data))
        except KeyError:
            return self.fail(404, "no such account")
        scheduler.nudge()
        self.json_out({"id": eid, "pending": True})

    def api_evedit(self, conn, session, event_id):
        if self.command == "DELETE":
            try:
                feeds.delete_event(conn, event_id)
            except KeyError:
                return self.fail(404, "no such event")
            scheduler.nudge()
            return self.json_out({"id": event_id, "deleting": True})
        try:
            feeds.update_event(conn, event_id, **self._event_fields(self.body_json()))
        except KeyError:
            return self.fail(404, "no such event")
        scheduler.nudge()
        self.json_out({"id": event_id, "pending": True})

    def api_calendar(self, conn, session):
        """A month of events for the grid, addressed by month rather than by
        a day count -- the caller is drawing a calendar, not a horizon.

        The connected accounts come with it. The form has to name one when
        there are several, and it cannot name what it has not been told.
        """
        q = self.query()
        start = (q.get("from") or [""])[0][:10]
        end = (q.get("to") or [""])[0][:10]
        if not (len(start) == 10 and len(end) == 10):
            raise ValueError("from and to are required, as YYYY-MM-DD")
        self.json_out({
            "from": start, "to": end,
            "accounts": [{"id": a["id"], "email": a["email"]}
                         for a in settings.list_google_accounts(conn)],
            "repeats": sorted(feeds.REPEATS),
            "reminders": list(feeds.REMINDERS),
            "events": feeds.events_between(conn, start + "T00:00:00",
                                           end + "T23:59:59")})

    def api_inbox(self, conn, session):
        floor = settings.get(conn, "inbox_min_importance")
        min_imp = self._int_arg("min_importance", floor, 0, 5)
        include = (self.query().get("archived") or [""])[0] == "1"
        days = settings.get(conn, "inbox_read_days")
        # `retired=1` asks for the ones that have aged out as well, which is
        # what the screen's own control sends. Retiring mail is a default for
        # the list, not a wall around it.
        if (self.query().get("retired") or [""])[0] == "1":
            days = 0
        messages = feeds.inbox(conn, min_importance=min_imp, limit=100,
                               include_archived=include, read_days=days)
        # How many the filter is holding back, so the screen can say so
        # rather than quietly showing less than it has.
        hidden = 0
        if days:
            hidden = len(feeds.inbox(conn, min_importance=min_imp, limit=100,
                                     include_archived=include)) - len(messages)
        self.json_out({
            "min_importance": min_imp,
            "read_days": days,
            "retired_hidden": max(0, hidden),
            "messages": messages})

    def api_syncst(self, conn, session):
        """Per-source freshness, so a stale feed is visible rather than quiet."""
        self.json_out({"sources": feeds.sync_status(conn),
                       "accounts": settings.list_google_accounts(conn),
                       # Silently dropping an edit is the same as losing it,
                       # so both numbers are reported: what is still queued,
                       # and what was given up on.
                       "pending": push.pending_count(conn),
                       "abandoned": push.abandoned_count(conn),
                       "unsent": feeds.unsent(conn)})

    def api_msgbody(self, conn, session, message_id):
        """One message, as text and as blocks. Fetched once, then kept."""
        def fetch(account_id, source_uid):
            return google_api.fetch_message(conn, account_id, source_uid)
        try:
            text, blocks, cached = feeds.message_body(conn, message_id, fetch)
        except KeyError:
            return self.fail(404, "no such message")
        except google_api.GoogleError as e:
            return self.fail(502, str(e))

        # Image sources are rewritten here rather than at parse time: the
        # signature depends on a key this process holds, and a stored URL
        # would be stale the moment that key changed.
        show = settings.get(conn, "load_remote_images")
        out = []
        for b in blocks:
            if b.get("t") == "img":
                src = b.get("src", "")
                if src.startswith("data:"):
                    out.append(b)
                elif show:
                    out.append({**b, "src": imageproxy.signed_path(conn, src)})
                else:
                    out.append({**b, "src": "", "blocked": True})
            else:
                out.append(b)
        self.json_out({"id": message_id, "body": text, "blocks": out,
                       "cached": cached, "images_on": bool(show)})

    # ── themes ────────────────────────────────────────────────────────────
    def api_themeact(self, conn, session):
        """What the page should wear, asked for before anything is drawn.

        Unauthenticated on purpose: the sign-in screen should already be
        wearing the chosen theme, and a palette reveals nothing about anyone.
        """
        active = settings.get(conn, "active_theme")
        theme = themes.get(conn, active) if active else None
        self.json_out({"id": theme["id"] if theme else "",
                       "base": theme["base"] if theme else "dark",
                       "tokens": theme["tokens"] if theme else {}})

    def api_themes(self, conn, session):
        if self.command == "GET":
            active = settings.get(conn, "active_theme")
            return self.json_out({
                "active": active,
                "preview": themes.PREVIEW,
                "themes": [{**t, "active": t["id"] == active}
                           for t in themes.all_themes(conn)]})
        # POST: import. The body is the theme file itself, so a file can be
        # handed over unchanged rather than wrapped in an envelope first.
        try:
            theme = themes.parse(self._raw_body(themes.MAX_JSON))
        except themes.ThemeError as e:
            return self.fail(400, str(e))
        tid = themes.save(conn, theme)
        self.json_out({"id": tid, "name": theme["name"],
                       "tokens": len(theme["tokens"])})

    def api_theme1(self, conn, session, theme_id):
        if self.command == "DELETE":
            try:
                themes.delete(conn, theme_id)
            except KeyError:
                return self.fail(404, "no such theme")
            except themes.ThemeError as e:
                return self.fail(400, str(e))
            if settings.get(conn, "active_theme") == theme_id:
                # Deleting what you are wearing has to leave you wearing
                # something, or the next paint has no colours at all.
                settings.set_(conn, "active_theme", "")
            return self.json_out({"deleted": theme_id})
        if themes.get(conn, theme_id) is None:
            return self.fail(404, "no such theme")
        settings.set_(conn, "active_theme", theme_id)
        self.json_out({"active": theme_id})

    def api_image(self, conn, session):
        q = self.query()
        url = (q.get("u") or [""])[0]
        if not imageproxy.verify(conn, url, (q.get("s") or [""])[0]):
            # Not "wrong signature" -- an attacker learns nothing from a
            # refusal that does not distinguish its reasons.
            return self.fail(403, "not allowed")
        try:
            ctype, data = imageproxy.fetch(url)
        except imageproxy.Refused as e:
            return self.fail(502, str(e))
        self._send(200, data, ctype, {"Cache-Control": "private, max-age=86400"})

    def api_editmsg(self, conn, session, message_id):
        data = self.body_json()
        # Taken from feeds so the two cannot drift: a field added there and
        # forgotten here would be accepted by the model and silently dropped
        # by the endpoint.
        fields = {k: v for k, v in data.items()
                  if k in (feeds.PUSHABLE | feeds.LOCAL_ONLY)}
        if not fields:
            raise ValueError("nothing to change")
        try:
            feeds.set_message(conn, message_id, **fields)
        except KeyError:
            return self.fail(404, "no such message")
        # Straight to Google rather than at the next tick: archiving something
        # and watching it sit there reads as broken.
        if fields.keys() & feeds.PUSHABLE:
            scheduler.nudge()
        self.json_out({"id": message_id, "updated": sorted(fields)})

    # ── ingest: the n8n side ──────────────────────────────────────────────
    def _ingest_account(self, conn, data):
        account = str(data.get("account") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", account):
            raise ValueError("account is required")
        if conn.execute("SELECT 1 FROM google_accounts WHERE id=?",
                        (account,)).fetchone() is None:
            raise ValueError("no such connected account")
        return account

    def api_ing_ev(self, conn, session):
        data = self.body_json()
        account = self._ingest_account(conn, data)
        events = data.get("events")
        if not isinstance(events, list):
            raise ValueError("events must be a list")
        written, skipped = feeds.upsert_events(conn, account, events)
        feeds.note_sync(conn, "calendar:" + account, "ok",
                        cursor=str(data.get("cursor") or "") or None)
        self.json_out({"written": written, "skipped_local_edits": skipped})

    def api_ing_msg(self, conn, session):
        data = self.body_json()
        account = self._ingest_account(conn, data)
        messages = data.get("messages")
        if not isinstance(messages, list):
            raise ValueError("messages must be a list")
        written, skipped = feeds.upsert_messages(conn, account, messages)
        feeds.note_sync(conn, "mail:" + account, "ok",
                        cursor=str(data.get("cursor") or "") or None)
        self.json_out({"written": written, "skipped_local_edits": skipped})

    def _run_sync(self, conn, data):
        if not isinstance(data, dict):
            raise ValueError("body must be an object")
        account = str(data.get("account_id") or "") or None
        try:
            days = max(1, min(730, int(
                data.get("days_ahead", google_api.DAYS_AHEAD))))
        except (TypeError, ValueError):
            days = google_api.DAYS_AHEAD
        result = sync.run(conn, account_id=account, days_ahead=days)
        # A partial failure is still a 200: the caller asked for a sync and
        # got one, and the body says exactly which account did not answer.
        # Failing the whole call would make a scheduler retry the accounts
        # that already succeeded.
        self.json_out(result)

    def api_sync_ing(self, conn, session):
        """The scheduled entry point. No Google credential crosses this line.

        A workflow engine authenticates with the ingest key and asks for a
        sync; this process holds the refresh token, talks to Google, and
        returns counts. The scheduler never sees a token, so a compromised
        scheduler cannot read the mailbox it triggers.
        """
        self._run_sync(conn, self.body_json())

    def api_sync_ses(self, conn, session):
        """The "Sync now" button, for when waiting for the schedule is silly."""
        self._run_sync(conn, self.body_json())

    def api_ing_sync(self, conn, session):
        """Let a workflow report its own failure.

        A feed that stops is invisible otherwise: the screen shows the last
        data it received and nothing says it is stale.
        """
        data = self.body_json()
        source = str(data.get("source") or "")[:80]
        if not source:
            raise ValueError("source is required")
        feeds.note_sync(conn, source, str(data.get("status", "ok"))[:40],
                        error=str(data.get("error", "")),
                        cursor=str(data.get("cursor") or "") or None)
        self.json_out({"noted": source})

    def api_home(self, conn, session):
        """One summary per section, for the front page.

        Each section reports its own state, including having no data and why.
        A widget that renders nothing is indistinguishable from one that is
        broken, so each says which it is.
        """
        from . import ledger
        month = stats.this_month()
        accounts = settings.list_google_accounts(conn)
        connected = bool(accounts)

        # ── budget ────────────────────────────────────────────────────────
        cats = conn.execute(
            "SELECT id, name FROM categories WHERE is_income=0 AND hidden=0"
        ).fetchall()
        spent = budgeted = 0
        over, per_cat = [], []
        for c in cats:
            b = ledger.get_budget(conn, month, c["id"])
            a = max(0, -ledger.category_activity(conn, c["id"], month))
            budgeted += b
            spent += a
            bal = ledger.category_balance(conn, c["id"], month)
            if bal < 0:
                over.append({"name": c["name"], "over_cents": -bal})
            if b or a:
                per_cat.append({"id": c["id"], "name": c["name"],
                                "spent_cents": a, "budgeted_cents": b})
        over.sort(key=lambda x: -x["over_cents"])

        # Ranked by how much of the envelope is gone, because that is what
        # the widget's chart draws and what the glance is for: is anything
        # running away from me.
        #
        # It ranked by amount spent, which is a different question and picks
        # different categories. Only seven fit, so the seven largest crowded
        # out a small one at ninety-nine per cent -- the chart said
        # "proportion" and the selection said "size", and the one that
        # mattered was the one left out.
        #
        # A category with nothing budgeted has no proportion to be near, so
        # it sorts after the ones that do rather than being dropped: its
        # spending is real and belongs on screen somewhere.
        def used(c):
            if c["budgeted_cents"] > 0:
                return (0, -(c["spent_cents"] / c["budgeted_cents"]),
                        -c["spent_cents"])
            return (1, 0, -c["spent_cents"])

        per_cat.sort(key=used)

        # A month nobody has budgeted for yet is a different state from one
        # where nothing has been spent, and the widget must be able to tell
        # them apart -- "0 of 0, 0%" reads as broken rather than as "not
        # started".
        last = conn.execute(
            "SELECT MAX(month) m FROM budget_months WHERE budgeted_cents <> 0"
        ).fetchone()["m"]
        budget = {
            "month": month,
            "to_be_budgeted_cents": ledger.to_be_budgeted(conn, month),
            "budgeted_cents": budgeted,
            "spent_cents": spent,
            "categories": len(cats),
            "overspent": over[:3],
            "overspent_count": len(over),
            "has_data": bool(cats),
            "started": budgeted > 0 or spent > 0,
            "top": per_cat[:7],
            "last_budgeted_month": last,
        }

        # ── calendar and mail ─────────────────────────────────────────────
        # An empty list and an unconfigured integration look identical on
        # screen unless each says which it is.
        ev = feeds.agenda(conn, days=7, limit=12) if connected else []
        msgs = feeds.inbox(conn,
                           min_importance=settings.get(conn,
                                                       "inbox_min_importance"),
                           limit=8,
                           read_days=settings.get(conn, "inbox_read_days"),
                           ) if connected else []
        calendar = {
            "connected": connected, "count": len(ev),
            "reason": "" if connected else "no Google account connected",
            "events": [{"id": e["id"], "title": e["title"],
                        "starts_at": e["starts_at"], "ends_at": e["ends_at"],
                        "all_day": bool(e["all_day"]),
                        "location": e["location"]} for e in ev],
        }
        mail = {
            "connected": connected, "count": len(msgs),
            "reason": "" if connected else "no Google account connected",
            "messages": [{"id": m["id"], "subject": m["subject"],
                          "sender": m["sender"], "score": m["score"],
                          "reason": m["reason"],
                          "received_at": m["received_at"]} for m in msgs],
        }

        self.json_out({
            "month": month,
            "google": {"configured": auth_google.configured(conn),
                       "connected": connected,
                       "accounts": len(accounts)},
            "budget": budget, "calendar": calendar, "mail": mail,
        })

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
        plan_targets = savings.targets(conn, end_month=month,
                                       analyses=analyses)
        cats = conn.execute(
            "SELECT c.id, c.name, g.name gname FROM categories c"
            " JOIN category_groups g ON g.id = c.group_id"
            " WHERE c.is_income=0 AND c.hidden=0 ORDER BY g.sort, c.sort"
        ).fetchall()

        items, short = [], 0
        for c in cats:
            a = analyses.get(c["id"], {})
            budgeted = ledger.get_budget(conn, month, c["id"])
            # Two different questions, and they had been answering with the
            # same number. The estimate is what next month costs if nothing
            # changes, weighted towards recent months. The recommendation is
            # what to aim for instead, drawn from the months you actually
            # spent least in and leaving the fixed costs alone.
            estimate = a.get("estimate_cents") or 0
            target = plan_targets.get(c["id"])
            recommended = target[0] if target else a.get("suggested_cents")
            if not budgeted and not estimate and not recommended:
                continue
            if budgeted < estimate:
                short += 1
            items.append({
                "id": c["id"], "name": c["name"], "group": c["gname"],
                "budgeted": budgeted,
                "recommended": recommended,
                "estimate": estimate,
                # The stable historical figure, kept so the detail panel can
                # say what "normally" is next to what is coming.
                "typical": a.get("trimmed_mean_cents") or 0,
                "target_reason": target[1] if target else "",
                "activity": ledger.category_activity(conn, c["id"], month),
                # What has actually gone out this month, as a positive
                # figure. A net inflow (a refund larger than the spending)
                # clamps to zero rather than drawing a bar below the axis.
                "actual": max(0, -ledger.category_activity(conn, c["id"], month)),
                "balance": ledger.category_balance(conn, c["id"], month),
                "kind": a.get("kind", ""),
                "confidence": a.get("confidence", "none"),
                "sample_months": a.get("sample_months", 0),
            })

        # What is in the bank, and where the month lands if the budget is
        # kept. Separate from to_be_budgeted, which is about assigning money
        # rather than having it.
        projection = ledger.month_projection(conn, month)
        unset = conn.execute(
            "SELECT COUNT(*) n FROM accounts"
            " WHERE on_budget=1 AND closed=0 AND opening_balance_cents=0"
        ).fetchone()["n"]

        self.json_out({
            "month": month,
            "to_be_budgeted_cents": ledger.to_be_budgeted(conn, month),
            "savings": projection | {
                # A balance built only from imported rows is a fact about the
                # import, not the account. Said out loud, because the figure
                # looks equally confident either way.
                "accounts_without_opening": unset,
            },
            "budgeted_total_cents": sum(i["budgeted"] for i in items),
            "estimate_total_cents": sum(i["estimate"] for i in items),
            "typical_total_cents": sum(i["typical"] for i in items),
            "actual_total_cents": sum(i["actual"] for i in items),
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
        analyses = {a["category_id"]: a for a in rows}
        plan_targets = savings.targets(conn, end_month=month,
                                       analyses=analyses)
        self.json_out({
            "month": month,
            "totals": stats.totals(conn, end_month=month),
            # Which months these figures rest on, and which were left out.
            # A recommendation drawn from three of your six months is a fine
            # answer; presenting it as "your history" without saying so is
            # how it becomes unarguable.
            "coverage": stats.coverage_notes(conn, end_month=month),
            "suggestions": [{
                "category_id": r["category_id"], "name": r["name"],
                "group": r["group"], "kind": r["kind"],
                "confidence": r["confidence"],
                "sample_months": r["sample_months"],
                # What next month costs if nothing changes.
                "estimate_cents": r.get("estimate_cents") or 0,
                # What this normally costs -- the stable figure behind it.
                "typical_cents": r.get("trimmed_mean_cents") or 0,
                # What to aim for instead. A different question from either,
                # and it must not quietly answer with the same number.
                "suggested_cents": (
                    plan_targets[r["category_id"]][0]
                    if r["category_id"] in plan_targets
                    else r["suggested_cents"]),
                "target_reason": (
                    plan_targets[r["category_id"]][1]
                    if r["category_id"] in plan_targets else ""),
                "low_cents": r["low_cents"], "high_cents": r["high_cents"],
                "trend_pct": r["trend_pct"], "basis": r["basis"],
                "occurrences": r.get("occurrences", 0),
            } for r in rows]})

    def api_saveflex(self, conn, session, category_id):
        """Correct one category's flexibility. Yours is never overwritten."""
        data = self.body_json()
        savings.set_flexibility(conn, category_id,
                                data.get("flexibility"), source="you")
        self.json_out({"category_id": category_id,
                       "flexibility": data.get("flexibility")})

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
        target = savings.targets(conn, end_month=month, window=window,
                                 analyses={cat: a}).get(cat)
        flex_row = conn.execute(
            "SELECT flexibility FROM categories WHERE id=?", (cat,)).fetchone()
        self.json_out({
            "category_id": cat, "months": a["months"],
            "spend": [a["spend_by_month"][m] for m in a["months"]],
            "budgeted": [ledger.get_budget(conn, m, cat) for m in a["months"]],
            # Three separate answers, deliberately. What it normally costs,
            # what next month is heading for, and what to aim for instead.
            "typical_cents": a["trimmed_mean_cents"],
            "estimate_cents": a.get("estimate_cents") or 0,
            "suggested_cents": target[0] if target else a["suggested_cents"],
            "target_reason": target[1] if target else "",
            # What decides whether "aim for" is allowed below "heading for".
            "flexibility": flex_row["flexibility"] if flex_row else "",
            "low_cents": a["low_cents"], "high_cents": a["high_cents"],
            "kind": a["kind"], "confidence": a["confidence"],
            "sample_months": a["sample_months"], "basis": a["basis"],
        })

    # How much of an over-sized upload to swallow so the client can finish
    # sending and read the refusal. Bounded, because draining an unbounded
    # body on request is a way to be kept busy for free.
    DRAIN_CAP = 24 * 1024 * 1024

    def _raw_body(self, limit):
        """Read a raw upload, refusing anything over `limit`.

        Refusing before reading is the obvious implementation and it produces
        a broken pipe: the client is still writing when the response arrives,
        so its send fails and the browser reports a network error rather than
        the perfectly good message explaining the file is too large.

        So an over-sized body is drained first, up to a cap, and then
        refused -- and beyond that cap the connection is closed, because at
        that point the sender is not a browser with a large statement.
        """
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("no file was sent")
        if length > limit:
            if length <= self.DRAIN_CAP:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            else:
                self.close_connection = True
            raise ValueError("file is larger than %d MB"
                             % (limit // 1024 // 1024))
        return self.rfile.read(length)

    def api_upload(self, conn, session):
        """Parse a statement into a reviewable batch. Writes nothing yet.

        The file arrives as a raw body with the account and filename in the
        query string, rather than as multipart. Multipart would mean parsing
        a format designed for 1995 in order to move one file, and the browser
        can send the bytes directly.
        """
        from . import categorize
        from . import statements as st
        q = self.query()
        account = (q.get("account") or [""])[0]
        if not re.fullmatch(r"[0-9a-f]{32}", account):
            raise ValueError("an account is required")
        if conn.execute("SELECT 1 FROM accounts WHERE id=?",
                        (account,)).fetchone() is None:
            raise ValueError("no such account")
        filename = (q.get("filename") or ["statement"])[0][:200]

        blob = self._raw_body(st.MAX_BYTES)
        try:
            batch_id, meta = st.create_batch(conn, account, filename, blob)
        except st.ParseError as e:
            # The message names the column or value that failed, which is the
            # only thing that makes a rejected statement fixable.
            return self.fail(400, str(e))

        # Suggest categories from your own history. The model is only
        # consulted if it is switched on and configured.
        use_model = settings.get(conn, "enable_llm_categories")
        if use_model:
            os.environ["ANTHROPIC_API_KEY"] = settings.get(conn, "anthropic_api_key")
            os.environ["CLASSIFY_MODEL"] = settings.get(conn, "classify_model")
        try:
            categorize.apply_to_batch(conn, batch_id, use_model=bool(use_model))
        except Exception as e:
            # A classifier failure must not lose a parsed statement.
            storage.log("categorise on import failed: %s" % type(e).__name__)

        b = st.batch(conn, batch_id)
        self.json_out({"batch_id": batch_id, "state": b["state"],
                       "rows_total": b["rows_total"],
                       "rows_duplicate": b["rows_duplicate"],
                       "period": [b["period_start"], b["period_end"]],
                       "kind": meta.get("kind"),
                       "date_format": meta.get("date_format"),
                       "mapping": meta.get("mapping"),
                       "fingerprint": meta.get("fingerprint")}, 201)

    def api_batches(self, conn, session):
        rows = conn.execute(
            "SELECT b.id, b.filename, b.uploaded_at, b.period_start,"
            " b.period_end, b.rows_total, b.rows_duplicate, b.rows_imported,"
            # account_id as well as the name: the screen defaults the account
            # picker to whatever was imported last, and a name cannot select
            # an option.
            " b.state, b.account_id, a.name account FROM import_batches b"
            " JOIN accounts a ON a.id = b.account_id"
            " ORDER BY b.uploaded_at DESC LIMIT 25").fetchall()
        self.json_out({"batches": [dict(r) for r in rows]})

    def api_batch(self, conn, session, batch_id):
        from . import statements as st
        b = st.batch(conn, batch_id)
        if b is None:
            return self.fail(404, "no such batch")

        if self.command == "GET":
            return self.json_out({
                "batch": dict(b),
                "rows": [dict(r) for r in st.batch_rows(conn, batch_id)]})

        if self.command == "DELETE":
            st.discard_batch(conn, batch_id)
            return self.json_out({"discarded": True})

        # POST commits it.
        data = self.body_json() or {}
        if data.get("remember_mapping") and b["fingerprint"]:
            conn.execute(
                "UPDATE bank_mappings SET label=? WHERE fingerprint=?",
                (str(data.get("label", ""))[:80], b["fingerprint"]))
            conn.commit()
        written = st.commit_batch(conn, batch_id)
        self.json_out({"imported": written,
                       "coverage": st.coverage(conn, b["account_id"]),
                       "gaps": st.gaps(conn, b["account_id"])})

    def api_batchrow(self, conn, session, row_id):
        from . import statements as st
        data = self.body_json()
        fields = {k: v for k, v in data.items()
                  if k in ("excluded", "category_id", "payee", "notes")}
        if not fields:
            raise ValueError("nothing to change")
        try:
            st.set_row(conn, row_id, **fields)
        except ValueError as e:
            return self.fail(400, str(e))
        self.json_out({"id": row_id, "updated": sorted(fields)})

    def api_txns(self, conn, session):
        """Recent transactions, optionally for one category or month.

        Split children rather than their parents, because the question being
        asked here is "what did this category buy", and the parent carries no
        category.
        """
        q = self.query()
        where = ["t.deleted=0"]
        args = []
        cat = (q.get("category") or [""])[0]
        if cat:
            if not re.fullmatch(r"[0-9a-f]{32}", cat):
                raise ValueError("category must be an id")
            where.append("t.category_id=?")
            args.append(cat)
        month = (q.get("month") or [""])[0]
        if month:
            if not re.fullmatch(r"\d{4}-\d{2}", month):
                raise ValueError("month must be YYYY-MM")
            where.append("substr(t.date,1,7)=?")
            args.append(month)
        try:
            limit = max(1, min(200, int((q.get("limit") or ["50"])[0])))
        except ValueError:
            raise ValueError("limit must be a number") from None

        rows = conn.execute(
            # category_id and parent_id come too: the split editor needs the
            # first to preselect, and the second to know this row is already
            # part of a split and cannot be divided again.
            "SELECT t.id, t.date, t.amount_cents, t.payee, t.notes, t.cleared,"
            "       t.source, t.category_id, t.parent_id, t.transfer_id,"
            "       a.name account, c.name category"
            " FROM transactions t JOIN accounts a ON a.id = t.account_id"
            " LEFT JOIN categories c ON c.id = t.category_id"
            " WHERE " + " AND ".join(where) +
            " ORDER BY t.date DESC, t.rowid DESC LIMIT ?",
            [*args, limit]).fetchall()
        self.json_out({"transactions": [dict(r) for r in rows]})

    def api_g_check(self, conn, session):
        """Report whether Google is usable right now.

        Exists so a credential saved in the interface can be confirmed
        without restarting anything -- the credentials are read per request,
        so this reflects the state the next sign-in will actually see.
        """
        cid = auth_google.client_id(conn)
        self.json_out({
            "configured": auth_google.configured(conn),
            "has_client_id": bool(cid),
            "has_client_secret": bool(auth_google.client_secret(conn)),
            # Enough to spot a wrong project pasted in, without echoing it.
            "client_id_hint": (cid[:12] + "…" + cid[-18:]) if len(cid) > 34 else cid,
            "redirect_uri": auth_google.redirect_uri(conn),
        })

    def api_g_connect(self, conn, session):
        """Begin consent for an account whose mail and calendar we may read."""
        if not auth_google.configured(conn):
            raise ValueError("Google is not configured on this deployment")
        url = auth_google.begin(conn, purpose="connect", return_to="/#/settings")
        self._redirect(url)

    def api_g_accts(self, conn, session):
        self.json_out({"accounts": settings.list_google_accounts(conn),
                       "configured": auth_google.configured(conn)})

    def api_g_acct(self, conn, session, account_id):
        n = settings.disconnect_google(conn, account_id)
        if not n:
            return self.fail(404, "no such account")
        self.json_out({"disconnected": True})

    def api_settings(self, conn, session):
        if self.command == "GET":
            return self.json_out({
                "settings": settings.all_for_display(conn),
                "secrets_available": crypt.available(),
                "google": {
                    "configured": auth_google.configured(conn),
                    "accounts": settings.list_google_accounts(conn),
                },
            })

        data = self.body_json()
        if not isinstance(data, dict) or not data:
            raise ValueError("nothing to change")
        unknown = [k for k in data if settings.kind_of(k) is None]
        if unknown:
            raise ValueError("unknown setting: %s" % ", ".join(sorted(unknown)))
        try:
            for key, value in data.items():
                settings.set_(conn, key, value)
        except RuntimeError as e:
            # No encryption key configured; refusing beats storing in clear.
            return self.fail(409, str(e))
        self.json_out({"updated": sorted(data)})

    def api_setting(self, conn, session, key):
        try:
            settings.clear(conn, key)
        except KeyError:
            return self.fail(404, "no such setting")
        self.json_out({"cleared": key})

    def api_groups(self, conn, session):
        from . import ledger
        if self.command == "GET":
            rows = conn.execute(
                "SELECT id, name, is_income, sort FROM category_groups"
                " ORDER BY sort, name").fetchall()
            return self.json_out({"groups": [dict(r) for r in rows]})
        data = self.body_json()
        name = str(data.get("name", "")).strip()
        if not name:
            raise ValueError("a group needs a name")
        try:
            gid = ledger.create_category_group(
                conn, name, is_income=bool(data.get("is_income")),
                sort=int(data.get("sort", 0)))
        except sqlite3.IntegrityError:
            raise ValueError("a group with that name already exists") from None
        self.json_out({"id": gid, "name": name}, 201)

    def api_cats(self, conn, session):
        from . import ledger
        if self.command == "GET":
            rows = ledger.list_categories(
                conn, include_hidden=(self.query().get("hidden") == ["1"]))
            return self.json_out({"categories": [dict(r) for r in rows]})

        data = self.body_json()
        name = str(data.get("name", "")).strip()
        group_id = data.get("group_id")
        if not name:
            raise ValueError("a category needs a name")
        if not isinstance(group_id, str) or not re.fullmatch(r"[0-9a-f]{32}",
                                                             group_id or ""):
            raise ValueError("a category needs a group")
        if conn.execute("SELECT 1 FROM category_groups WHERE id=?",
                        (group_id,)).fetchone() is None:
            raise ValueError("no such group")
        try:
            # A category in an income group is income unless told otherwise:
            # putting "Salary" under Income and having it count as spending
            # is not a distinction anyone means to draw.
            grp_income = bool(conn.execute(
                "SELECT is_income FROM category_groups WHERE id=?",
                (group_id,)).fetchone()["is_income"])
            cid = ledger.create_category(
                conn, group_id, name,
                is_income=bool(data.get("is_income", grp_income)),
                carryover_negative=bool(data.get("carryover_negative")),
                sort=int(data.get("sort", 0)))
        except sqlite3.IntegrityError:
            raise ValueError("that group already has a category with that name") \
                from None
        # A new category is immediately available to the classifier: it reads
        # the category table directly, so there is nothing else to update.
        self.json_out({"id": cid, "name": name, "group_id": group_id}, 201)

    def api_cat(self, conn, session, category_id):
        from . import ledger
        if self.command == "DELETE":
            try:
                outcome = ledger.delete_category(conn, category_id)
            except KeyError:
                return self.fail(404, "no such category")
            # Anything with history is hidden rather than removed, so past
            # spending keeps its meaning.
            return self.json_out({"outcome": outcome})

        data = self.body_json()
        fields = {k: v for k, v in data.items()
                  if k in ("name", "group_id", "sort", "hidden",
                           "carryover_negative", "is_income")}
        if not fields:
            raise ValueError("nothing to change")
        try:
            ledger.update_category(conn, category_id, **fields)
        except KeyError:
            return self.fail(404, "no such category")
        self.json_out({"id": category_id, "updated": sorted(fields)})

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
