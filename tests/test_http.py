"""End-to-end checks against a real server socket.

Header and CSRF behaviour is asserted here rather than by inspecting
constants, because what matters is what actually reaches the browser.
"""

import http.client
import json
import threading

import pytest

from dashboard import auth, schema, server, storage


@pytest.fixture
def live(tmp_path):
    storage.configure(str(tmp_path / "data"))
    # The server uses one connection per thread; the fixture's own connection
    # is separate and used only to seed the user.
    conn = storage.connect()
    schema.migrate(conn)
    auth.create_user(conn, "king", "hunter2hunter2")
    conn.close()

    httpd, port = server.bind("127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield port
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    hdrs = dict(headers or {})
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    c.request(method, path, body=payload, headers=hdrs)
    r = c.getresponse()
    raw = r.read()
    try:
        parsed = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        parsed = raw
    return r.status, dict(r.getheaders()), parsed


def login(port):
    status, headers, body = call(port, "POST", "/api/auth/login",
                                 {"username": "king", "password": "hunter2hunter2"})
    assert status == 200, body
    cookie = headers["Set-Cookie"].split(";")[0]
    return cookie, body["csrf_token"]


# ── headers ───────────────────────────────────────────────────────────────
def test_security_headers_on_a_normal_response(live):
    _, h, _ = call(live, "GET", "/api/health")
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "DENY"
    assert h["Referrer-Policy"] == "no-referrer"
    assert "Content-Security-Policy" in h


def test_headers_are_present_on_errors_too(live):
    # An error path that forgets the headers is an error path an attacker
    # will aim for.
    for path in ("/api/view/budget?month=2026-01", "/nope"):
        _, h, _ = call(live, "GET", path)
        assert "Content-Security-Policy" in h, path
        assert h["X-Content-Type-Options"] == "nosniff", path


def test_csp_forbids_inline_script(live):
    _, h, _ = call(live, "GET", "/api/health")
    csp = h["Content-Security-Policy"]
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert "default-src 'none'" in csp


def test_no_hsts_when_not_served_over_tls(live):
    # Pinning HSTS from a plain-HTTP development run would lock the developer
    # out of their own machine on that port.
    _, h, _ = call(live, "GET", "/api/health")
    assert "Strict-Transport-Security" not in h


# ── authentication ────────────────────────────────────────────────────────
def test_protected_routes_reject_anonymous_callers(live):
    for method, path in (("GET", "/api/view/budget?month=2026-01"),
                         ("GET", "/api/accounts"),
                         ("POST", "/api/accounts")):
        status, _, _ = call(live, method, path, {} if method == "POST" else None)
        assert status == 401, (method, path)


def test_login_sets_an_httponly_samesite_cookie(live):
    _, headers, body = call(live, "POST", "/api/auth/login",
                            {"username": "king", "password": "hunter2hunter2"})
    sc = headers["Set-Cookie"]
    assert "HttpOnly" in sc, "script must not be able to read the session id"
    assert "SameSite=Strict" in sc
    assert "csrf_token" in body


def test_bad_credentials_say_nothing_useful(live):
    _, _, a = call(live, "POST", "/api/auth/login",
                   {"username": "king", "password": "wrong"})
    _, _, b = call(live, "POST", "/api/auth/login",
                   {"username": "ghost", "password": "wrong"})
    assert a == b == {"error": "invalid username or password"}


def test_session_cookie_grants_access(live):
    cookie, _ = login(live)
    status, _, body = call(live, "GET", "/api/view/budget?month=2026-01",
                           headers={"Cookie": cookie})
    assert status == 200
    assert body["to_be_budgeted_cents"] == 0


# ── CSRF ──────────────────────────────────────────────────────────────────
def test_mutation_without_a_csrf_token_is_refused(live):
    cookie, _ = login(live)
    status, _, body = call(live, "POST", "/api/accounts", {"name": "Checking"},
                           headers={"Cookie": cookie})
    assert status == 403 and "csrf" in body["error"].lower()


def test_mutation_with_a_wrong_csrf_token_is_refused(live):
    cookie, _ = login(live)
    status, _, _ = call(live, "POST", "/api/accounts", {"name": "Checking"},
                        headers={"Cookie": cookie, "X-CSRF-Token": "nope"})
    assert status == 403


def test_mutation_with_the_right_token_succeeds(live):
    cookie, csrf = login(live)
    status, _, body = call(live, "POST", "/api/accounts", {"name": "Checking"},
                           headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 201 and "id" in body


def test_reads_do_not_require_a_csrf_token(live):
    cookie, _ = login(live)
    status, _, _ = call(live, "GET", "/api/accounts", headers={"Cookie": cookie})
    assert status == 200


def test_one_sessions_token_does_not_work_for_another(live):
    cookie_a, _ = login(live)
    _, csrf_b = login(live)
    status, _, _ = call(live, "POST", "/api/accounts", {"name": "X"},
                        headers={"Cookie": cookie_a, "X-CSRF-Token": csrf_b})
    assert status == 403


# ── logout ────────────────────────────────────────────────────────────────
def test_logout_invalidates_the_session_server_side(live):
    cookie, csrf = login(live)
    status, headers, _ = call(live, "POST", "/api/auth/logout",
                              headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 200 and "Max-Age=0" in headers["Set-Cookie"]
    # The cookie is cleared client-side, but the session must be gone from the
    # server too -- otherwise a copied cookie still works after logout.
    status, _, _ = call(live, "GET", "/api/accounts", headers={"Cookie": cookie})
    assert status == 401


# ── routing and input ─────────────────────────────────────────────────────
def test_wrong_method_is_405_not_404(live):
    status, _, _ = call(live, "DELETE", "/api/health")
    assert status == 405


def test_static_traversal_is_refused(live):
    for path in ("/../pyproject.toml", "/../../etc/passwd",
                 "/%2e%2e/pyproject.toml"):
        status, _, _ = call(live, "GET", path)
        assert status in (400, 404), path


def test_index_is_served(live):
    status, _, _ = call(live, "GET", "/")
    assert status == 200


def test_bad_json_is_a_client_error(live):
    c = http.client.HTTPConnection("127.0.0.1", live, timeout=5)
    c.request("POST", "/api/auth/login", body=b"{not json",
              headers={"Content-Type": "application/json"})
    assert c.getresponse().status == 400


def test_oversized_body_is_refused(live):
    cookie, csrf = login(live)
    status, _, _ = call(live, "POST", "/api/accounts",
                        {"name": "x" * (server.MAX_BODY + 10)},
                        headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 400


def test_month_parameter_is_validated(live):
    cookie, _ = login(live)
    for bad in ("", "2026", "2026-1", "abcd-ef", "2026-01-01"):
        status, _, _ = call(live, "GET", "/api/view/budget?month=" + bad,
                            headers={"Cookie": cookie})
        assert status == 400, bad


# ── cookie construction ───────────────────────────────────────────────────
def test_cookie_is_marked_secure_only_when_tls_is_declared():
    from dashboard import security
    assert "Secure" in security.cookie("s", "v", secure=True)
    # Marking Secure on a plain-HTTP dev run means the browser silently drops
    # the cookie and login appears to do nothing.
    assert "Secure" not in security.cookie("s", "v", secure=False)


def test_expire_cookie_clears_it():
    from dashboard import security
    assert "Max-Age=0" in security.expire_cookie("s")


def test_csrf_compare_rejects_empties():
    from dashboard import security
    assert not security.csrf_ok("", "")
    assert not security.csrf_ok(None, "x")
    assert not security.csrf_ok("x", None)
    assert security.csrf_ok("token", "token")
    assert not security.csrf_ok("token", "token ")
