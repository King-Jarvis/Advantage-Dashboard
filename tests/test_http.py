"""End-to-end checks against a real server socket.

Header and CSRF behaviour is asserted here rather than by inspecting
constants, because what matters is what actually reaches the browser.
"""

import http.client
import json
import threading
import urllib.parse

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
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Not every response is text -- the image proxy returns bytes.
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


# ── Google sign-in routes ─────────────────────────────────────────────────
def test_config_reports_google_off_when_unconfigured(live, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    status, _, body = call(live, "GET", "/api/config")
    assert status == 200 and body["google_enabled"] is False


def test_config_never_leaks_the_client_id(live, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "shh")
    _, _, body = call(live, "GET", "/api/config")
    # Still exact: this endpoint is unauthenticated, so every field on it is
    # a deliberate decision. needs_setup is install state, not a credential.
    assert body == {"google_enabled": True, "needs_setup": False}, \
        "config should say only whether, not what"


def test_google_start_is_unavailable_when_unconfigured(live, monkeypatch):
    for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
              "GOOGLE_CLIENT_SECRET_PATH"):
        monkeypatch.delenv(k, raising=False)
    status, _, _ = call(live, "GET", "/api/auth/google/start")
    assert status == 503


def test_google_start_redirects_to_google(live, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "shh")
    monkeypatch.setenv("BASE_URL", "https://dash.example")
    status, h, _ = call(live, "GET", "/api/auth/google/start")
    assert status == 303
    loc = h["Location"]
    assert loc.startswith("https://accounts.google.com/")
    assert "code_challenge_method=S256" in loc
    assert "client_secret" not in loc, "the secret must never reach the browser"


def test_callback_with_a_denial_lands_somewhere_sensible(live):
    status, h, _ = call(live, "GET",
                        "/api/auth/google/callback?error=access_denied")
    assert status == 303 and h["Location"] == "/?auth=denied"


def test_callback_with_an_unknown_state_is_refused(live):
    # A replayed or forged callback must not authenticate anyone.
    status, h, _ = call(live, "GET",
                        "/api/auth/google/callback?code=x&state=never-issued")
    assert status == 303 and h["Location"] == "/?auth=failed"
    assert "Set-Cookie" not in h, "a failed callback must not open a session"


def test_callback_without_a_code_is_refused(live):
    status, h, _ = call(live, "GET", "/api/auth/google/callback?state=abc")
    assert status == 303 and h["Location"] == "/?auth=failed"


# ── connection reuse ──────────────────────────────────────────────────────
def test_several_requests_on_one_connection(live):
    """HTTP/1.1 keep-alive: one handler instance serves the whole connection.

    Per-request state has to be reset for each, or the second request looks
    already-answered, nothing is written, and the browser waits forever on a
    connection that will never speak again. Every other test here opens a
    fresh connection, so only this one sees it.
    """
    c = http.client.HTTPConnection("127.0.0.1", live, timeout=5)
    for path in ("/api/health", "/", "/app.js", "/api/health", "/styles.css"):
        c.request("GET", path)
        r = c.getresponse()
        r.read()
        assert r.status == 200, path
        assert r.getheader("Content-Security-Policy"), path
    c.close()


def test_static_then_api_on_one_connection(live):
    # The original failure was specific to a static response followed by
    # anything else: the API path happened to mask it.
    c = http.client.HTTPConnection("127.0.0.1", live, timeout=5)
    c.request("GET", "/app.js")
    c.getresponse().read()
    c.request("GET", "/api/health")
    r = c.getresponse()
    body = r.read()
    assert r.status == 200 and b"ok" in body
    c.close()


def test_a_404_does_not_poison_the_connection(live):
    c = http.client.HTTPConnection("127.0.0.1", live, timeout=5)
    c.request("GET", "/no-such-file.js")
    assert c.getresponse().read() is not None
    c.request("GET", "/api/health")
    r = c.getresponse()
    r.read()
    assert r.status == 200
    c.close()


# ── budget editing ────────────────────────────────────────────────────────
def _seed_budget(port, cookie, csrf):
    """An account, a group and a category, via the API where possible."""
    from dashboard import ledger, storage
    conn = storage.connect()
    acct = ledger.create_account(conn, "Checking")
    grp = ledger.create_category_group(conn, "Everyday")
    cat = ledger.create_category(conn, grp, "Groceries")
    inc = ledger.create_category_group(conn, "Income", is_income=True)
    sal = ledger.create_category(conn, inc, "Salary", is_income=True)
    ledger.add_transaction(conn, acct, "2026-08-01", 1000_00, "Pay", sal)
    other = ledger.create_category(conn, grp, "Fuel")
    conn.close()
    return {"acct": acct, "cat": cat, "other": other}


def test_setting_a_budget_updates_to_be_budgeted(live):
    cookie, csrf = login(live)
    ids = _seed_budget(live, cookie, csrf)
    status, _, body = call(
        live, "PATCH", f"/api/edit/budget/2026-08/{ids['cat']}",
        {"budgeted_cents": 400_00},
        headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 200
    assert body["budgeted_cents"] == 400_00
    assert body["to_be_budgeted_cents"] == 600_00


def test_a_budget_must_be_integer_cents(live):
    cookie, csrf = login(live)
    ids = _seed_budget(live, cookie, csrf)
    for bad in (400.5, "400", None):
        status, _, _ = call(
            live, "PATCH", f"/api/edit/budget/2026-08/{ids['cat']}",
            {"budgeted_cents": bad},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf})
        assert status == 400, bad


def test_budget_edits_need_csrf(live):
    cookie, _ = login(live)
    ids = _seed_budget(live, cookie, None)
    status, _, _ = call(
        live, "PATCH", f"/api/edit/budget/2026-08/{ids['cat']}",
        {"budgeted_cents": 100}, headers={"Cookie": cookie})
    assert status == 403


def test_a_malformed_month_or_category_is_a_404_not_a_crash(live):
    cookie, csrf = login(live)
    for path in ("/api/edit/budget/2026-8/abc", "/api/edit/budget/xxxx-xx/" + "a" * 32):
        status, _, _ = call(live, "PATCH", path, {"budgeted_cents": 1},
                            headers={"Cookie": cookie, "X-CSRF-Token": csrf})
        assert status in (400, 404), path


def test_moving_money_preserves_the_months_total(live):
    cookie, csrf = login(live)
    ids = _seed_budget(live, cookie, csrf)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    call(live, "PATCH", f"/api/edit/budget/2026-08/{ids['cat']}",
         {"budgeted_cents": 300_00}, headers=h)
    call(live, "PATCH", f"/api/edit/budget/2026-08/{ids['other']}",
         {"budgeted_cents": 100_00}, headers=h)
    _, _, before = call(live, "GET", "/api/view/budget?month=2026-08",
                        headers={"Cookie": cookie})

    status, _, body = call(
        live, "POST", "/api/edit/budget/2026-08/move",
        {"from_category": ids["cat"], "to_category": ids["other"],
         "cents": 50_00}, headers=h)
    assert status == 200
    assert body["from"]["budgeted_cents"] == 250_00
    assert body["to"]["budgeted_cents"] == 150_00
    # Moving between envelopes must not change how much is unassigned.
    assert body["to_be_budgeted_cents"] == before["to_be_budgeted_cents"]


def test_moving_rejects_nonsense(live):
    cookie, csrf = login(live)
    ids = _seed_budget(live, cookie, csrf)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    for payload in (
        {"from_category": ids["cat"], "to_category": ids["other"], "cents": 0},
        {"from_category": ids["cat"], "to_category": ids["other"], "cents": -5},
        {"from_category": ids["cat"], "to_category": ids["cat"], "cents": 100},
        {"from_category": "nope", "to_category": ids["other"], "cents": 100},
        {"cents": 100},
    ):
        status, _, _ = call(live, "POST", "/api/edit/budget/2026-08/move",
                            payload, headers=h)
        assert status == 400, payload


# ── suggestions and history ───────────────────────────────────────────────
def test_suggestions_are_served(live):
    cookie, csrf = login(live)
    _seed_budget(live, cookie, csrf)
    status, _, body = call(live, "GET", "/api/view/suggestions?month=2026-08",
                           headers={"Cookie": cookie})
    assert status == 200
    assert "totals" in body and isinstance(body["suggestions"], list)


def test_history_requires_a_real_category_id(live):
    cookie, _ = login(live)
    for bad in ("", "abc", "z" * 32):
        status, _, _ = call(
            live, "GET", f"/api/view/history?month=2026-08&category={bad}",
            headers={"Cookie": cookie})
        assert status == 400, bad


def test_history_window_is_clamped(live):
    cookie, csrf = login(live)
    ids = _seed_budget(live, cookie, csrf)
    _, _, body = call(
        live, "GET",
        f"/api/view/history?month=2026-08&category={ids['cat']}&months=9999",
        headers={"Cookie": cookie})
    # An unbounded window would let a request walk the whole table.
    assert len(body["months"]) <= 36


def test_coverage_is_served(live):
    cookie, _ = login(live)
    status, _, body = call(live, "GET", "/api/view/coverage",
                           headers={"Cookie": cookie})
    assert status == 200
    assert "covered" in body and "gaps" in body


def test_budget_endpoints_are_closed_to_anonymous_callers(live):
    for method, path, payload in (
        ("GET", "/api/view/suggestions?month=2026-08", None),
        ("GET", "/api/view/coverage", None),
        ("POST", "/api/edit/budget/2026-08/move", {}),
    ):
        status, _, _ = call(live, method, path, payload)
        assert status == 401, path


# ── categories ────────────────────────────────────────────────────────────
def test_creating_a_group_then_a_category(live):
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    status, _, grp = call(live, "POST", "/api/category-groups",
                          {"name": "Everyday"}, headers=h)
    assert status == 201
    status, _, cat = call(live, "POST", "/api/categories",
                          {"name": "Coffee", "group_id": grp["id"]}, headers=h)
    assert status == 201 and cat["name"] == "Coffee"

    _, _, listing = call(live, "GET", "/api/categories",
                         headers={"Cookie": cookie})
    assert [c["name"] for c in listing["categories"]] == ["Coffee"]


def test_a_category_needs_a_name_and_a_real_group(live):
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    _, _, grp = call(live, "POST", "/api/category-groups",
                     {"name": "G"}, headers=h)
    for payload in ({"group_id": grp["id"]},
                    {"name": "  ", "group_id": grp["id"]},
                    {"name": "X"},
                    {"name": "X", "group_id": "nope"},
                    {"name": "X", "group_id": "f" * 32}):
        status, _, _ = call(live, "POST", "/api/categories", payload, headers=h)
        assert status == 400, payload


def test_duplicate_names_in_one_group_are_refused(live):
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    _, _, grp = call(live, "POST", "/api/category-groups", {"name": "G"}, headers=h)
    call(live, "POST", "/api/categories",
         {"name": "Coffee", "group_id": grp["id"]}, headers=h)
    status, _, _ = call(live, "POST", "/api/categories",
                        {"name": "Coffee", "group_id": grp["id"]}, headers=h)
    assert status == 400


def test_renaming_and_hiding_a_category(live):
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    _, _, grp = call(live, "POST", "/api/category-groups", {"name": "G"}, headers=h)
    _, _, cat = call(live, "POST", "/api/categories",
                     {"name": "Cofee", "group_id": grp["id"]}, headers=h)
    status, _, _ = call(live, "PATCH", f"/api/categories/{cat['id']}",
                        {"name": "Coffee"}, headers=h)
    assert status == 200

    call(live, "PATCH", f"/api/categories/{cat['id']}", {"hidden": True}, headers=h)
    _, _, visible = call(live, "GET", "/api/categories", headers={"Cookie": cookie})
    assert visible["categories"] == []
    _, _, all_cats = call(live, "GET", "/api/categories?hidden=1",
                          headers={"Cookie": cookie})
    assert [c["name"] for c in all_cats["categories"]] == ["Coffee"]


def test_a_category_with_history_is_hidden_not_deleted(live):
    """Deleting it would leave past transactions pointing at nothing."""
    from dashboard import ledger, storage
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    _, _, grp = call(live, "POST", "/api/category-groups", {"name": "G"}, headers=h)
    _, _, cat = call(live, "POST", "/api/categories",
                     {"name": "Coffee", "group_id": grp["id"]}, headers=h)

    conn = storage.connect()
    acct = ledger.create_account(conn, "Chk")
    ledger.add_transaction(conn, acct, "2026-08-01", -350, "Roasters", cat["id"])
    conn.close()

    _, _, body = call(live, "DELETE", f"/api/categories/{cat['id']}", headers=h)
    assert body["outcome"] == "hidden"


def test_an_unused_category_is_deleted_outright(live):
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    _, _, grp = call(live, "POST", "/api/category-groups", {"name": "G"}, headers=h)
    _, _, cat = call(live, "POST", "/api/categories",
                     {"name": "Scratch", "group_id": grp["id"]}, headers=h)
    _, _, body = call(live, "DELETE", f"/api/categories/{cat['id']}", headers=h)
    assert body["outcome"] == "deleted"


def test_category_changes_need_csrf(live):
    cookie, csrf = login(live)
    _, _, grp = call(live, "POST", "/api/category-groups", {"name": "G"},
                     headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    status, _, _ = call(live, "POST", "/api/categories",
                        {"name": "X", "group_id": grp["id"]},
                        headers={"Cookie": cookie})
    assert status == 403


# ── settings ──────────────────────────────────────────────────────────────
def test_settings_are_closed_to_anonymous_callers(live):
    for method, path, payload in (("GET", "/api/settings", None),
                                  ("PATCH", "/api/settings", {"timezone": "UTC"}),
                                  ("GET", "/api/google/accounts", None)):
        status, _, _ = call(live, method, path, payload)
        assert status == 401, path


def test_settings_round_trip(live):
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    status, _, body = call(live, "GET", "/api/settings",
                           headers={"Cookie": cookie})
    assert status == 200
    keys = {s["key"] for s in body["settings"]}
    assert "enable_llm_categories" in keys and "classify_model" in keys

    status, _, _ = call(live, "PATCH", "/api/settings",
                        {"timezone": "Europe/London",
                         "baseline_window_months": 18}, headers=h)
    assert status == 200
    _, _, after = call(live, "GET", "/api/settings", headers={"Cookie": cookie})
    got = {s["key"]: s.get("value") for s in after["settings"]}
    assert got["timezone"] == "Europe/London"
    assert got["baseline_window_months"] == 18


def test_unknown_settings_are_refused(live):
    cookie, csrf = login(live)
    status, _, _ = call(live, "PATCH", "/api/settings", {"rm_rf": "yes"},
                        headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 400


def test_settings_changes_need_csrf(live):
    cookie, _ = login(live)
    status, _, _ = call(live, "PATCH", "/api/settings", {"timezone": "UTC"},
                        headers={"Cookie": cookie})
    assert status == 403


def test_a_stored_secret_is_never_sent_to_the_browser(
        live, tmp_path, monkeypatch):
    from dashboard import crypt
    key = tmp_path / "k"
    key.write_text("a-long-random-secret-for-this-test-only")
    monkeypatch.setenv("TOKEN_KEY_PATH", str(key))
    crypt.reset_for_tests()

    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    status, _, _ = call(live, "PATCH", "/api/settings",
                        {"anthropic_api_key": "FAKE-ANTHROPIC-SECRET"}, headers=h)
    assert status == 200

    _, _, body = call(live, "GET", "/api/settings", headers={"Cookie": cookie})
    blob = json.dumps(body)
    # Not even an authenticated session can read a credential back out.
    assert "FAKE-ANTHROPIC-SECRET" not in blob
    item = next(s for s in body["settings"] if s["key"] == "anthropic_api_key")
    assert item["value"] is None and item["is_set"] is True
    crypt.reset_for_tests()


def test_disconnecting_an_unknown_google_account_is_404(live):
    cookie, csrf = login(live)
    status, _, _ = call(live, "DELETE", "/api/google/accounts/" + "a" * 32,
                        headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 404


# ── credentials take effect without a restart ─────────────────────────────
def test_saving_google_credentials_takes_effect_immediately(live, tmp_path,
                                                            monkeypatch):
    """The whole point of the settings page.

    Credentials are read per request rather than captured at start-up, so a
    value saved in the interface is live on the next call. Anything else means
    telling someone to restart a server to finish a form.
    """
    from dashboard import crypt
    key = tmp_path / "k"
    key.write_text("a-long-random-secret-for-this-test-only")
    monkeypatch.setenv("TOKEN_KEY_PATH", str(key))
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET_PATH", raising=False)
    crypt.reset_for_tests()

    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}

    _, _, before = call(live, "GET", "/api/google/check",
                        headers={"Cookie": cookie})
    assert before["configured"] is False

    status, _, _ = call(live, "PATCH", "/api/settings", {
        "google_client_id": "123456789012-abcdefghijklmnop.apps.googleusercontent.com",
        "google_client_secret": "FAKE-GOOGLE-SECRET",
    }, headers=h)
    assert status == 200

    _, _, after = call(live, "GET", "/api/google/check",
                       headers={"Cookie": cookie})
    assert after["configured"] is True
    assert after["has_client_id"] and after["has_client_secret"]
    crypt.reset_for_tests()


def test_the_check_never_echoes_the_secret(live, tmp_path, monkeypatch):
    from dashboard import crypt
    key = tmp_path / "k"
    key.write_text("another-long-random-secret-value-here")
    monkeypatch.setenv("TOKEN_KEY_PATH", str(key))
    crypt.reset_for_tests()
    cookie, csrf = login(live)
    call(live, "PATCH", "/api/settings",
         {"google_client_secret": "FAKE-GOOGLE-SECRET-2"},
         headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    _, _, body = call(live, "GET", "/api/google/check",
                      headers={"Cookie": cookie})
    assert "FAKE-GOOGLE-SECRET-2" not in json.dumps(body)
    assert body["has_client_secret"] is True
    crypt.reset_for_tests()


def test_the_check_needs_a_session(live):
    status, _, _ = call(live, "GET", "/api/google/check")
    assert status == 401


# ── statement import ──────────────────────────────────────────────────────
CSV_UPLOAD = (b"Date,Description,Amount\n"
              b"2026-09-04,SUPERSTORE 991,-42.15\n"
              b"2026-09-05,PAYROLL ACME,2500.00\n"
              b"2026-09-06,SUPERSTORE 991,-18.40\n")


def _account(name="Checking"):
    from dashboard import ledger, storage
    conn = storage.connect()
    aid = ledger.create_account(conn, name)
    conn.close()
    return aid


def upload(port, cookie, csrf, account, body=CSV_UPLOAD, filename="sep.csv"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST",
              f"/api/import/upload?account={account}&filename={filename}",
              body=body,
              headers={"Cookie": cookie, "X-CSRF-Token": csrf,
                       "Content-Type": "text/csv"})
    r = c.getresponse()
    raw = r.read()
    return r.status, json.loads(raw) if raw else None


def test_uploading_parses_but_writes_nothing_to_the_ledger(live):
    from dashboard import ledger, storage
    cookie, csrf = login(live)
    acct = _account()
    status, body = upload(live, cookie, csrf, acct)
    assert status == 201
    assert body["rows_total"] == 3

    conn = storage.connect()
    assert ledger.account_balance(conn, acct) == 0, "review comes before writing"
    conn.close()


def test_committing_writes_the_rows(live):
    from dashboard import ledger, storage
    cookie, csrf = login(live)
    acct = _account()
    _, body = upload(live, cookie, csrf, acct)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    status, _, res = call(live, "POST", f"/api/import/batch/{body['batch_id']}",
                          {}, headers=h)
    assert status == 200 and res["imported"] == 3

    conn = storage.connect()
    assert ledger.account_balance(conn, acct) == -4215 + 250000 - 1840
    conn.close()


def test_a_row_can_be_excluded_before_committing(live):
    cookie, csrf = login(live)
    acct = _account()
    _, body = upload(live, cookie, csrf, acct)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    _, _, detail = call(live, "GET", f"/api/import/batch/{body['batch_id']}",
                        headers={"Cookie": cookie})
    row = detail["rows"][0]
    call(live, "PATCH", f"/api/import/row/{row['id']}",
         {"excluded": True}, headers=h)
    _, _, res = call(live, "POST", f"/api/import/batch/{body['batch_id']}",
                     {}, headers=h)
    assert res["imported"] == 2


def test_discarding_leaves_nothing_behind(live):
    from dashboard import ledger, storage
    cookie, csrf = login(live)
    acct = _account()
    _, body = upload(live, cookie, csrf, acct)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    call(live, "DELETE", f"/api/import/batch/{body['batch_id']}", headers=h)
    _, _, detail = call(live, "GET", f"/api/import/batch/{body['batch_id']}",
                        headers={"Cookie": cookie})
    assert detail["rows"] == []
    conn = storage.connect()
    assert ledger.account_balance(conn, acct) == 0
    conn.close()


def test_the_second_upload_of_a_file_flags_every_row(live):
    cookie, csrf = login(live)
    acct = _account()
    _, first = upload(live, cookie, csrf, acct)
    call(live, "POST", f"/api/import/batch/{first['batch_id']}", {},
         headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    _, second = upload(live, cookie, csrf, acct)
    assert second["rows_duplicate"] == second["rows_total"]


def test_an_unparseable_file_is_refused_with_a_reason(live):
    cookie, csrf = login(live)
    acct = _account()
    status, body = upload(live, cookie, csrf, acct,
                          body=b"just,some,columns\n1,2,3\n")
    # The message has to name what failed, or a rejected statement is a dead
    # end rather than something to fix.
    assert status == 400
    assert "date" in body["error"].lower()


def test_upload_requires_a_real_account(live):
    cookie, csrf = login(live)
    for acct in ("", "nope", "f" * 32):
        status, _ = upload(live, cookie, csrf, acct)
        assert status == 400, acct


def test_an_oversized_upload_is_refused(live):
    from dashboard import statements as st
    cookie, csrf = login(live)
    acct = _account()
    status, _ = upload(live, cookie, csrf, acct,
                       body=b"x" * (st.MAX_BYTES + 10))
    assert status == 400


def test_import_endpoints_are_closed_to_anonymous_callers(live):
    acct = _account()
    c = http.client.HTTPConnection("127.0.0.1", live, timeout=10)
    c.request("POST", f"/api/import/upload?account={acct}", body=b"x")
    assert c.getresponse().status == 401
    status, _, _ = call(live, "GET", "/api/import/batches")
    assert status == 401


def test_committing_needs_csrf(live):
    cookie, csrf = login(live)
    acct = _account()
    _, body = upload(live, cookie, csrf, acct)
    status, _, _ = call(live, "POST", f"/api/import/batch/{body['batch_id']}",
                        {}, headers={"Cookie": cookie})
    assert status == 403


# ── calendar and mail feeds ───────────────────────────────────────────────
def _google_account(tmp_path, monkeypatch):
    from dashboard import crypt, settings, storage
    k = tmp_path / "gk"
    k.write_text("a-long-random-secret-for-this-test-only")
    monkeypatch.setenv("TOKEN_KEY_PATH", str(k))
    crypt.reset_for_tests()
    conn = storage.connect()
    aid = settings.save_google_account(conn, "sub-1", "a@example.com",
                                       "FAKE-REFRESH", "FAKE-ACCESS", None, "s")
    conn.close()
    return aid


def test_ingest_needs_the_ingest_key_not_a_session(live, tmp_path, monkeypatch):
    acct = _google_account(tmp_path, monkeypatch)
    cookie, csrf = login(live)
    # A browser session must never reach an ingest route.
    status, _, _ = call(live, "POST", "/api/ingest/events",
                        {"account": acct, "events": []},
                        headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 401
    from dashboard import crypt
    crypt.reset_for_tests()


def test_ingested_events_reach_the_agenda(live, tmp_path, monkeypatch):
    import os

    from dashboard import crypt
    acct = _google_account(tmp_path, monkeypatch)
    monkeypatch.setenv("INGEST_KEY", "test-ingest-key")
    os.environ["INGEST_KEY"] = "test-ingest-key"

    # Within the agenda's horizon: an event far enough ahead is correctly
    # excluded, which is behaviour rather than a bug to test around.
    import datetime
    soon = (datetime.datetime.now() + datetime.timedelta(days=2)).strftime(
        "%Y-%m-%dT09:00:00")
    status, _, body = call(live, "POST", "/api/ingest/events", {
        "account": acct,
        "events": [{"source_uid": "e1", "title": "Standup", "starts_at": soon}],
    }, headers={"X-Ingest-Key": "test-ingest-key"})
    assert status == 200 and body["written"] == 1

    cookie, _ = login(live)
    _, _, agenda = call(live, "GET", "/api/view/agenda?days=7",
                        headers={"Cookie": cookie})
    assert [e["title"] for e in agenda["events"]] == ["Standup"]
    del os.environ["INGEST_KEY"]
    crypt.reset_for_tests()


def test_correcting_an_importance_survives_the_next_sync(live, tmp_path,
                                                         monkeypatch):
    import os

    from dashboard import crypt
    acct = _google_account(tmp_path, monkeypatch)
    os.environ["INGEST_KEY"] = "test-ingest-key"
    h_ing = {"X-Ingest-Key": "test-ingest-key"}

    call(live, "POST", "/api/ingest/messages", {
        "account": acct,
        "messages": [{"source_uid": "m1", "subject": "Invoice",
                      "received_at": "2026-09-01T09:00:00", "importance": 1}],
    }, headers=h_ing)

    cookie, csrf = login(live)
    _, _, inbox = call(live, "GET", "/api/view/inbox?min_importance=0",
                       headers={"Cookie": cookie})
    mid = inbox["messages"][0]["id"]
    call(live, "PATCH", f"/api/edit/message/{mid}", {"importance_override": 5},
         headers={"Cookie": cookie, "X-CSRF-Token": csrf})

    # A later sync re-asserts the classifier's low score and brings fresh
    # state from the provider. The row is written -- an importance correction
    # is ours alone, so it must not block Gmail's own changes from landing --
    # and the correction survives because upsert never touches the override
    # column, not because the row was skipped.
    _, _, again = call(live, "POST", "/api/ingest/messages", {
        "account": acct,
        "messages": [{"source_uid": "m1", "subject": "Invoice paid",
                      "received_at": "2026-09-01T09:00:00", "importance": 1,
                      "is_unread": False}],
    }, headers=h_ing)
    assert again["written"] == 1 and again["skipped_local_edits"] == 0

    _, _, fresh = call(live, "GET", "/api/view/inbox?min_importance=0",
                       headers={"Cookie": cookie})
    landed = next(m for m in fresh["messages"] if m["id"] == mid)
    assert landed["subject"] == "Invoice paid", "provider update was blocked"
    assert landed["is_unread"] == 0

    _, _, after = call(live, "GET", "/api/view/inbox?min_importance=4",
                       headers={"Cookie": cookie})
    assert [m["id"] for m in after["messages"]] == [mid]
    del os.environ["INGEST_KEY"]
    crypt.reset_for_tests()


def test_ingest_refuses_an_unknown_account(live, monkeypatch):
    import os
    os.environ["INGEST_KEY"] = "test-ingest-key"
    status, _, _ = call(live, "POST", "/api/ingest/events",
                        {"account": "f" * 32, "events": []},
                        headers={"X-Ingest-Key": "test-ingest-key"})
    assert status == 400
    del os.environ["INGEST_KEY"]


def test_a_workflow_can_report_its_own_failure(live, monkeypatch):
    import os
    os.environ["INGEST_KEY"] = "test-ingest-key"
    call(live, "POST", "/api/ingest/sync",
         {"source": "mail:x", "status": "error", "error": "invalid_grant"},
         headers={"X-Ingest-Key": "test-ingest-key"})
    cookie, _ = login(live)
    _, _, st = call(live, "GET", "/api/view/status", headers={"Cookie": cookie})
    # A feed that stops must be visible, not just quiet.
    assert st["sources"][0]["last_error"] == "invalid_grant"
    del os.environ["INGEST_KEY"]


# ── the scheduler's door ──────────────────────────────────────────────────
def test_sync_route_takes_the_ingest_key_and_refuses_a_session(live, tmp_path,
                                                               monkeypatch):
    """The whole point of the design: n8n gets a key that triggers a sync and
    can do nothing else. If a session also opened this route, a stolen ingest
    key and a stolen cookie would be interchangeable."""
    import os

    from dashboard import crypt
    _google_account(tmp_path, monkeypatch)
    os.environ["INGEST_KEY"] = "test-ingest-key"

    cookie, csrf = login(live)
    status, _, _ = call(live, "POST", "/api/sync/google", {},
                        headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 401, "a browser session reached the scheduler's route"

    status, _, _ = call(live, "POST", "/api/sync/google", {},
                        headers={"X-Ingest-Key": "wrong"})
    assert status == 401

    del os.environ["INGEST_KEY"]
    crypt.reset_for_tests()


def test_sync_now_button_takes_a_session_and_refuses_the_ingest_key(
        live, tmp_path, monkeypatch):
    import os

    from dashboard import crypt
    _google_account(tmp_path, monkeypatch)
    os.environ["INGEST_KEY"] = "test-ingest-key"

    status, _, _ = call(live, "POST", "/api/action/sync", {},
                        headers={"X-Ingest-Key": "test-ingest-key"})
    assert status == 401, "an ingest key reached a browser route"

    del os.environ["INGEST_KEY"]
    crypt.reset_for_tests()


def test_sync_with_no_connected_account_is_reported_not_crashed(live,
                                                                monkeypatch):
    import os
    os.environ["INGEST_KEY"] = "test-ingest-key"
    status, _, body = call(live, "POST", "/api/sync/google", {},
                           headers={"X-Ingest-Key": "test-ingest-key"})
    # A scheduler needs a parseable answer, not a 500 it can only log.
    assert status == 200 and body["ok"] is False
    assert "no connected" in body["error"]
    del os.environ["INGEST_KEY"]


def test_sync_reports_a_partial_failure_as_200_with_detail(live, tmp_path,
                                                           monkeypatch):
    """A partial failure must not fail the whole call: retrying would re-run
    the accounts that already succeeded."""
    import os

    from dashboard import crypt, google_api
    _google_account(tmp_path, monkeypatch)
    os.environ["INGEST_KEY"] = "test-ingest-key"

    def boom(*a, **k):
        raise google_api.GoogleError("revoked")

    monkeypatch.setattr(google_api, "fetch_events", boom)
    status, _, body = call(live, "POST", "/api/sync/google", {},
                           headers={"X-Ingest-Key": "test-ingest-key"})
    assert status == 200 and body["ok"] is False
    assert body["results"][0]["ok"] is False
    # The reason reaches the caller, but no token does.
    assert "revoked" in body["results"][0]["error"]
    assert "FAKE-REFRESH" not in str(body)

    del os.environ["INGEST_KEY"]
    crypt.reset_for_tests()


# ── first run, over HTTP ──────────────────────────────────────────────────
@pytest.fixture
def fresh(tmp_path):
    """A server with no users at all -- an install nobody has claimed."""
    storage.configure(str(tmp_path / "fresh-data"))
    conn = storage.connect()
    schema.migrate(conn)
    conn.close()
    httpd, port = server.bind("127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield port
    httpd.shutdown()
    httpd.server_close()
    from dashboard import firstrun
    firstrun.clear_token()


def test_a_fresh_install_advertises_setup(fresh):
    _, _, cfg = call(fresh, "GET", "/api/config")
    assert cfg["needs_setup"] is True


def test_a_claimed_install_does_not_advertise_setup(live):
    # Separate tests on purpose: both fixtures configure the one global
    # storage root, so a test holding both would have them fight over it.
    _, _, cfg = call(live, "GET", "/api/config")
    assert cfg["needs_setup"] is False


def test_config_never_leaks_the_setup_token(fresh):
    """The code is proof you can read a file on the box. Serving it over HTTP
    to anyone who asks would defeat the entire point."""
    from dashboard import firstrun
    from dashboard import storage as st
    conn = st.connect()
    token = firstrun.ensure_token(conn)
    conn.close()
    _, _, cfg = call(fresh, "GET", "/api/config")
    assert token not in json.dumps(cfg)
    status, _, body = call(fresh, "GET", "/api/setup/claim")
    assert status in (404, 405), "the claim route answered a GET"


def test_claiming_over_http_signs_you_in(fresh):
    from dashboard import firstrun
    from dashboard import storage as st
    conn = st.connect()
    token = firstrun.ensure_token(conn)
    conn.close()

    status, headers, body = call(fresh, "POST", "/api/setup/claim", {
        "token": token, "username": "king", "password": "a-long-enough-pass"})
    assert status == 200, body
    assert body["csrf_token"]
    cookie = headers["Set-Cookie"].split(";")[0]

    # The session works immediately -- no second sign-in.
    status, _, who = call(fresh, "GET", "/api/auth/whoami", headers={"Cookie": cookie})
    assert status == 200 and who["username"] == "king"

    _, _, cfg = call(fresh, "GET", "/api/config")
    assert cfg["needs_setup"] is False


def test_a_wrong_code_over_http_creates_nothing(fresh):
    from dashboard import firstrun
    from dashboard import storage as st
    conn = st.connect()
    firstrun.ensure_token(conn)
    conn.close()
    status, _, body = call(fresh, "POST", "/api/setup/claim", {
        "token": "wrong", "username": "intruder", "password": "a-long-pass-x"})
    assert status == 400
    _, _, cfg = call(fresh, "GET", "/api/config")
    assert cfg["needs_setup"] is True, "a bad code claimed the install"


def test_claim_is_refused_once_a_user_exists(live):
    """The route stays mounted, so it has to refuse on its own."""
    status, _, body = call(live, "POST", "/api/setup/claim", {
        "token": "anything", "username": "intruder", "password": "a-long-pass"})
    assert status == 400
    assert "already been set up" in str(body)


def test_the_ingest_key_can_be_set_in_settings(live, tmp_path, monkeypatch):
    """The whole setup is meant to be doable in a browser, so the key n8n uses
    has to be settable there rather than only in the environment."""
    import os

    from dashboard import crypt, settings
    from dashboard import storage as st
    k = tmp_path / "ik"
    k.write_text("a-long-random-secret-for-this-test-only")
    monkeypatch.setenv("TOKEN_KEY_PATH", str(k))
    crypt.reset_for_tests()
    os.environ.pop("INGEST_KEY", None)

    conn = st.connect()
    settings.set_(conn, "ingest_key", "key-from-the-settings-page")
    conn.close()

    status, _, _ = call(live, "POST", "/api/sync/google", {},
                        headers={"X-Ingest-Key": "key-from-the-settings-page"})
    assert status == 200, "a key set in Settings was not accepted"
    status, _, _ = call(live, "POST", "/api/sync/google", {},
                        headers={"X-Ingest-Key": "the-wrong-key"})
    assert status == 401
    crypt.reset_for_tests()


def test_calendar_endpoint_needs_a_range_and_returns_it(live, tmp_path,
                                                        monkeypatch):
    from dashboard import crypt, feeds
    from dashboard import storage as st
    acct = _google_account(tmp_path, monkeypatch)
    conn = st.connect()
    feeds.upsert_events(conn, acct, [
        {"source_uid": "a", "starts_at": "2026-09-10T09:00:00", "title": "Standup"},
        {"source_uid": "b", "starts_at": "2026-12-01T09:00:00", "title": "Later"},
    ])
    conn.close()
    cookie, _ = login(live)

    status, _, body = call(live, "GET",
                           "/api/view/calendar?from=2026-09-01&to=2026-09-30",
                           headers={"Cookie": cookie})
    assert status == 200
    assert [e["title"] for e in body["events"]] == ["Standup"]

    # A grid without a range is a bug in the caller, not a default worth
    # guessing at.
    status, _, _ = call(live, "GET", "/api/view/calendar",
                        headers={"Cookie": cookie})
    assert status == 400
    crypt.reset_for_tests()


def test_calendar_needs_a_session(live):
    status, _, _ = call(live, "GET",
                        "/api/view/calendar?from=2026-09-01&to=2026-09-30")
    assert status == 401


# ── reading a message ─────────────────────────────────────────────────────
def test_a_message_body_is_fetched_then_served_from_cache(live, tmp_path,
                                                          monkeypatch):
    from dashboard import crypt, feeds, google_api
    from dashboard import storage as st
    acct = _google_account(tmp_path, monkeypatch)
    conn = st.connect()
    feeds.upsert_messages(conn, acct, [
        {"source_uid": "m1", "received_at": "2026-09-01T09:00:00",
         "subject": "Hello", "thread_id": "t1"}])
    mid = conn.execute("SELECT id FROM messages").fetchone()[0]
    conn.close()

    calls = []
    monkeypatch.setattr(google_api, "fetch_message",
                        lambda c, a, u: calls.append(u) or ("the text", []))

    cookie, _ = login(live)
    status, _, body = call(live, "GET", f"/api/view/message/{mid}",
                           headers={"Cookie": cookie})
    assert status == 200 and body["body"] == "the text" and body["cached"] is False

    _, _, again = call(live, "GET", f"/api/view/message/{mid}",
                       headers={"Cookie": cookie})
    assert again["cached"] is True
    assert calls == ["m1"], "Gmail was asked twice for the same body"
    crypt.reset_for_tests()


def test_an_unknown_message_body_is_404(live):
    cookie, _ = login(live)
    status, _, _ = call(live, "GET", "/api/view/message/" + "0" * 32,
                        headers={"Cookie": cookie})
    assert status == 404


def test_a_message_body_needs_a_session(live):
    status, _, _ = call(live, "GET", "/api/view/message/" + "0" * 32)
    assert status == 401


def test_a_gmail_failure_is_reported_not_swallowed(live, tmp_path, monkeypatch):
    from dashboard import crypt, feeds, google_api
    from dashboard import storage as st
    acct = _google_account(tmp_path, monkeypatch)
    conn = st.connect()
    feeds.upsert_messages(conn, acct, [
        {"source_uid": "m2", "received_at": "2026-09-01T09:00:00"}])
    mid = conn.execute("SELECT id FROM messages").fetchone()[0]
    conn.close()

    def boom(*a, **k):
        raise google_api.GoogleError("Gmail said no")
    monkeypatch.setattr(google_api, "fetch_message", boom)

    cookie, _ = login(live)
    status, _, body = call(live, "GET", f"/api/view/message/{mid}",
                           headers={"Cookie": cookie})
    assert status == 502 and "Gmail said no" in str(body)
    crypt.reset_for_tests()


def test_trash_and_spam_reach_the_editor(live, tmp_path, monkeypatch):
    from dashboard import crypt, feeds
    from dashboard import storage as st
    acct = _google_account(tmp_path, monkeypatch)
    conn = st.connect()
    feeds.upsert_messages(conn, acct, [
        {"source_uid": "m3", "received_at": "2026-09-01T09:00:00"}])
    mid = conn.execute("SELECT id FROM messages").fetchone()[0]
    conn.close()

    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    status, _, body = call(live, "PATCH", f"/api/edit/message/{mid}",
                           {"trashed": 1, "is_spam": 1}, headers=h)
    assert status == 200 and set(body["updated"]) == {"trashed", "is_spam"}

    conn = st.connect()
    r = conn.execute("SELECT trashed, is_spam, dirty FROM messages").fetchone()
    conn.close()
    assert r["trashed"] == 1 and r["is_spam"] == 1 and r["dirty"] == 1
    crypt.reset_for_tests()


# ── the image proxy, over HTTP ────────────────────────────────────────────
def test_the_image_endpoint_refuses_an_unsigned_url(live):
    cookie, _ = login(live)
    status, _, _ = call(live, "GET",
                        "/api/image?u=http%3A%2F%2F127.0.0.1%2Fx&s=nope",
                        headers={"Cookie": cookie})
    assert status == 403


def test_the_image_endpoint_needs_a_session(live):
    """Signed or not, an unauthenticated fetcher is still a fetcher."""
    status, _, _ = call(live, "GET", "/api/image?u=https%3A%2F%2Fx%2Fa.jpg&s=x")
    assert status == 401


def test_a_signed_internal_url_is_still_refused(live):
    """Signing proves we emitted it, not that it is safe to fetch. Both
    checks have to hold."""
    from dashboard import imageproxy
    from dashboard import storage as st
    conn = st.connect()
    url = "http://127.0.0.1:8766/api/config"
    sig = imageproxy.sign(conn, url)
    conn.close()

    cookie, _ = login(live)
    status, _, body = call(
        live, "GET",
        f"/api/image?u={urllib.parse.quote(url, safe='')}&s={sig}",
        headers={"Cookie": cookie})
    assert status == 502 and "inside the network" in str(body)


def test_a_signed_public_url_is_fetched(live, monkeypatch):
    from dashboard import imageproxy
    from dashboard import storage as st
    conn = st.connect()
    url = "https://cdn.example.com/a.png"
    sig = imageproxy.sign(conn, url)
    conn.close()

    monkeypatch.setattr(imageproxy, "fetch", lambda u: ("image/png", b"\x89PNG"))
    cookie, _ = login(live)
    status, headers, body = call(
        live, "GET",
        f"/api/image?u={urllib.parse.quote(url, safe='')}&s={sig}",
        headers={"Cookie": cookie})
    assert status == 200
    assert headers["Content-Type"] == "image/png"
    assert body == b"\x89PNG"


def test_images_can_be_turned_off(live, tmp_path, monkeypatch):
    from dashboard import crypt, feeds, google_api, settings
    from dashboard import storage as st
    acct = _google_account(tmp_path, monkeypatch)
    conn = st.connect()
    feeds.upsert_messages(conn, acct, [
        {"source_uid": "m9", "received_at": "2026-09-01T09:00:00"}])
    mid = conn.execute("SELECT id FROM messages").fetchone()[0]
    settings.set_(conn, "load_remote_images", False)
    conn.close()

    monkeypatch.setattr(google_api, "fetch_message", lambda c, a, u: (
        "text", [{"t": "img", "src": "https://cdn.example.com/a.jpg", "alt": "hat"}]))

    cookie, _ = login(live)
    _, _, body = call(live, "GET", f"/api/view/message/{mid}",
                      headers={"Cookie": cookie})
    img = next(b for b in body["blocks"] if b["t"] == "img")
    assert img["blocked"] is True and img["src"] == ""
    # The address is not handed to the browser either, so nothing can load it.
    assert "cdn.example.com" not in json.dumps(body)
    crypt.reset_for_tests()


# ── editing the calendar ──────────────────────────────────────────────────
def test_creating_an_event_over_http(live, tmp_path, monkeypatch):
    from dashboard import crypt
    _google_account(tmp_path, monkeypatch)
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}

    status, _, body = call(live, "POST", "/api/edit/event", {
        "title": "Dentist", "starts_at": "2026-09-15T14:00:00-05:00",
        "location": "Hill St"}, headers=h)
    assert status == 200 and body["pending"] is True

    _, _, cal = call(live, "GET",
                     "/api/view/calendar?from=2026-09-01&to=2026-09-30",
                     headers={"Cookie": cookie})
    ev = cal["events"][0]
    assert ev["title"] == "Dentist" and ev["location"] == "Hill St"
    assert ev["starts_at"] == "2026-09-15T19:00:00", "offset not converted"
    assert ev["dirty"] == 1, "a new event should be waiting to be sent"
    crypt.reset_for_tests()


def test_editing_and_deleting_an_event(live, tmp_path, monkeypatch):
    from dashboard import crypt
    _google_account(tmp_path, monkeypatch)
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}

    _, _, made = call(live, "POST", "/api/edit/event",
                      {"title": "Old", "starts_at": "2026-09-15T14:00:00"},
                      headers=h)
    eid = made["id"]

    status, _, _ = call(live, "PATCH", f"/api/edit/event/{eid}",
                        {"title": "New"}, headers=h)
    assert status == 200
    _, _, cal = call(live, "GET",
                     "/api/view/calendar?from=2026-09-01&to=2026-09-30",
                     headers={"Cookie": cookie})
    assert cal["events"][0]["title"] == "New"

    status, _, _ = call(live, "DELETE", f"/api/edit/event/{eid}", headers=h)
    assert status == 200
    # It leaves the grid at once, even though Google has not been told yet.
    _, _, cal = call(live, "GET",
                     "/api/view/calendar?from=2026-09-01&to=2026-09-30",
                     headers={"Cookie": cookie})
    assert cal["events"] == []
    crypt.reset_for_tests()


def test_an_invalid_event_is_refused(live, tmp_path, monkeypatch):
    from dashboard import crypt
    _google_account(tmp_path, monkeypatch)
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    status, _, body = call(live, "POST", "/api/edit/event", {
        "title": "x", "starts_at": "2026-09-15T14:00:00",
        "ends_at": "2026-09-15T13:00:00"}, headers=h)
    assert status == 400 and "end before it starts" in str(body)
    crypt.reset_for_tests()


def test_editing_an_unknown_event_is_404(live):
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    status, _, _ = call(live, "PATCH", "/api/edit/event/" + "0" * 32,
                        {"title": "x"}, headers=h)
    assert status == 404


def test_event_editing_needs_a_session(live):
    status, _, _ = call(live, "POST", "/api/edit/event", {"title": "x"})
    assert status == 401


def test_the_account_is_required_when_there_are_several(live, tmp_path,
                                                        monkeypatch):
    """With one account it can be inferred; with two, guessing would put the
    event in the wrong calendar."""
    from dashboard import crypt, settings
    from dashboard import storage as st
    _google_account(tmp_path, monkeypatch)
    conn = st.connect()
    settings.save_google_account(conn, "sub-2", "b@example.com",
                                 "FAKE-REFRESH-2", "FAKE-ACCESS-2", None, "s")
    conn.close()
    cookie, csrf = login(live)
    status, _, body = call(live, "POST", "/api/edit/event",
                           {"title": "x", "starts_at": "2026-09-15T14:00:00"},
                           headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 400 and "account_id" in str(body)
    crypt.reset_for_tests()


# ── themes over HTTP ──────────────────────────────────────────────────────
def test_the_active_theme_is_readable_without_a_session(live):
    """The sign-in screen should already be wearing the chosen theme, and a
    palette reveals nothing about anyone."""
    status, _, body = call(live, "GET", "/api/theme")
    assert status == 200
    assert "tokens" in body and body["base"] in ("dark", "light")


def test_the_catalog_needs_a_session(live):
    status, _, _ = call(live, "GET", "/api/themes")
    assert status == 401


def test_importing_activating_and_deleting_a_theme(live):
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}

    status, _, made = call(live, "POST", "/api/themes", {
        "name": "Painting", "base": "light",
        "tokens": {"--canvas": "#EDE6D8", "--text": "#000000"}}, headers=h)
    assert status == 200 and made["tokens"] == 2
    tid = made["id"]

    status, _, _ = call(live, "POST", f"/api/themes/{tid}", {}, headers=h)
    assert status == 200

    # It is now what an unauthenticated first paint would use.
    _, _, active = call(live, "GET", "/api/theme")
    assert active["base"] == "light"
    assert active["tokens"]["--canvas"] == "#EDE6D8"

    status, _, _ = call(live, "DELETE", f"/api/themes/{tid}", headers=h)
    assert status == 200
    # Deleting what you were wearing must leave you wearing something.
    _, _, after = call(live, "GET", "/api/theme")
    assert after["id"] == "" and after["tokens"] == {}


def test_a_hostile_theme_is_refused_with_a_usable_message(live):
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    status, _, body = call(live, "POST", "/api/themes", {
        "name": "Nasty", "base": "dark",
        "tokens": {"--canvas": "red; background: url(https://evil/x)"}}, headers=h)
    assert status == 400
    # Naming the token is the only way the author can fix it.
    assert "--canvas" in str(body)


def test_a_builtin_cannot_be_deleted_over_http(live):
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    _, _, cat = call(live, "GET", "/api/themes", headers={"Cookie": cookie})
    builtin = next(t for t in cat["themes"] if t["builtin"])
    status, _, _ = call(live, "DELETE", f"/api/themes/{builtin['id']}", headers=h)
    assert status == 400


def test_the_catalog_says_which_theme_is_worn(live):
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    _, _, cat = call(live, "GET", "/api/themes", headers={"Cookie": cookie})
    builtin = next(t for t in cat["themes"] if t["builtin"])
    call(live, "POST", f"/api/themes/{builtin['id']}", {}, headers=h)
    _, _, again = call(live, "GET", "/api/themes", headers={"Cookie": cookie})
    assert again["active"] == builtin["id"]
    assert next(t for t in again["themes"] if t["id"] == builtin["id"])["active"]


def test_creating_an_event_names_a_calendar(live, tmp_path, monkeypatch):
    """The bug this fixes: the form never sent one, and with two accounts
    connected every attempt was refused."""
    from dashboard import crypt, settings
    from dashboard import storage as st
    _google_account(tmp_path, monkeypatch)
    conn = st.connect()
    settings.save_google_account(conn, "sub-2", "b@example.com",
                                 "FAKE-REFRESH-2", "FAKE-ACCESS-2", None, "s")
    conn.close()

    cookie, csrf = login(live)
    # The grid hands the form the list, so it can name one.
    _, _, cal = call(live, "GET",
                     "/api/view/calendar?from=2026-09-01&to=2026-09-30",
                     headers={"Cookie": cookie})
    assert len(cal["accounts"]) == 2
    assert all("email" in a and "id" in a for a in cal["accounts"])

    status, _, body = call(live, "POST", "/api/edit/event", {
        "title": "Dentist", "starts_at": "2026-09-15T14:00:00",
        "account_id": cal["accounts"][0]["id"]},
        headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 200, body
    crypt.reset_for_tests()


def test_the_calendar_payload_carries_the_option_lists(live, tmp_path,
                                                       monkeypatch):
    """The form must not invent its own repeat values: they are validated
    server-side against exactly this list."""
    from dashboard import crypt, feeds
    _google_account(tmp_path, monkeypatch)
    cookie, _ = login(live)
    _, _, cal = call(live, "GET",
                     "/api/view/calendar?from=2026-09-01&to=2026-09-30",
                     headers={"Cookie": cookie})
    assert set(cal["repeats"]) == set(feeds.REPEATS)
    assert set(cal["reminders"]) == set(feeds.REMINDERS)
    crypt.reset_for_tests()


def test_a_repeat_the_form_did_not_offer_is_refused(live, tmp_path, monkeypatch):
    from dashboard import crypt
    _google_account(tmp_path, monkeypatch)
    cookie, csrf = login(live)
    status, _, body = call(live, "POST", "/api/edit/event", {
        "title": "x", "starts_at": "2026-09-15T14:00:00",
        "recurrence": "RRULE:FREQ=SECONDLY"},
        headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 400
    crypt.reset_for_tests()


def test_an_account_can_be_created_and_then_used(live):
    """The endpoint existed and nothing called it, so an empty ledger left the
    import screen with an empty dropdown and no way forward."""
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}

    _, _, before = call(live, "GET", "/api/accounts", headers={"Cookie": cookie})
    assert before["accounts"] == []

    status, _, made = call(live, "POST", "/api/accounts",
                           {"name": "UFCU Checking", "type": "checking"},
                           headers=h)
    assert status == 201 and made["id"]

    _, _, after = call(live, "GET", "/api/accounts", headers={"Cookie": cookie})
    assert [a["name"] for a in after["accounts"]] == ["UFCU Checking"]
    assert after["accounts"][0]["on_budget"] == 1


def test_an_investment_account_is_off_budget(live):
    """Money in one is not money you are about to spend, and counting it as
    such makes every envelope lie."""
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    call(live, "POST", "/api/accounts",
         {"name": "Brokerage", "type": "investment", "on_budget": False},
         headers=h)
    _, _, got = call(live, "GET", "/api/accounts", headers={"Cookie": cookie})
    assert got["accounts"][0]["on_budget"] == 0


def test_an_account_needs_a_name(live):
    cookie, csrf = login(live)
    status, _, _ = call(live, "POST", "/api/accounts", {"name": "  "},
                        headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 400


# ── renaming a category ───────────────────────────────────────────────────
def test_a_category_can_be_renamed_without_touching_its_history(live):
    """Transactions point at the id, so past months keep their meaning under
    the new name rather than being re-labelled or orphaned."""
    from dashboard import ledger
    from dashboard import storage as st
    conn = st.connect()
    gid = ledger.create_category_group(conn, "Everyday")
    cid = ledger.create_category(conn, gid, "Food")
    acct = ledger.create_account(conn, "Checking")
    ledger.add_transaction(conn, acct, "2026-08-01", -1200, "Tesco", cid)
    conn.close()

    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    status, _, _ = call(live, "PATCH", f"/api/categories/{cid}",
                        {"name": "Groceries"}, headers=h)
    assert status == 200

    conn = st.connect()
    row = conn.execute("SELECT name FROM categories WHERE id=?", (cid,)).fetchone()
    still = conn.execute("SELECT COUNT(*) FROM transactions WHERE category_id=?",
                         (cid,)).fetchone()[0]
    conn.close()
    assert row["name"] == "Groceries"
    assert still == 1, "renaming lost the transaction"


def test_a_category_can_be_moved_between_groups(live):
    from dashboard import ledger
    from dashboard import storage as st
    conn = st.connect()
    a = ledger.create_category_group(conn, "Everyday")
    b = ledger.create_category_group(conn, "Bills")
    cid = ledger.create_category(conn, a, "Water")
    conn.close()

    cookie, csrf = login(live)
    call(live, "PATCH", f"/api/categories/{cid}", {"group_id": b},
         headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    conn = st.connect()
    got = conn.execute("SELECT group_id FROM categories WHERE id=?",
                       (cid,)).fetchone()[0]
    conn.close()
    assert got == b


def test_an_empty_name_is_refused(live):
    from dashboard import ledger
    from dashboard import storage as st
    conn = st.connect()
    gid = ledger.create_category_group(conn, "Everyday")
    cid = ledger.create_category(conn, gid, "Food")
    conn.close()
    cookie, csrf = login(live)
    status, _, _ = call(live, "PATCH", f"/api/categories/{cid}", {"name": "   "},
                        headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 400


def test_bulk_categorising_obeys_the_setting_not_the_caller(live, monkeypatch):
    """A button that spends money while the switch governing it says off is
    the kind of thing you only discover on a bill."""
    from dashboard import categorize, settings
    from dashboard import storage as st
    called = []
    monkeypatch.setattr(categorize, "_ask_model",
                        lambda *a, **k: called.append(1) or {})

    conn = st.connect()
    settings.set_(conn, "enable_llm_categories", False)
    conn.close()

    cookie, csrf = login(live)
    # The caller asks for the model; the setting refuses.
    _, _, body = call(live, "POST", "/api/categorize", {"use_model": True},
                      headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    assert called == [], "asked the model with the setting switched off"
    assert body["model_allowed"] is False


# ── filing imported rows ──────────────────────────────────────────────────
def _ledger_with_unfiled(n=3):
    from dashboard import ledger
    from dashboard import storage as st
    conn = st.connect()
    gid = ledger.create_category_group(conn, "Everyday")
    cid = ledger.create_category(conn, gid, "Groceries")
    acct = ledger.create_account(conn, "Checking")
    for i in range(n):
        ledger.add_transaction(conn, acct, "2026-08-0%d" % (i + 1), -1000,
                               "TESCO METRO 4471%d" % i)
    ledger.add_transaction(conn, acct, "2026-08-09", -500, "SOMEWHERE ELSE")
    conn.close()
    return cid


def test_unfiled_rows_are_grouped_by_merchant(live):
    """Twelve payroll deposits are one decision, not twelve."""
    _ledger_with_unfiled(3)
    cookie, _ = login(live)
    _, _, got = call(live, "GET", "/api/view/unfiled", headers={"Cookie": cookie})
    keys = {g["key"]: g["count"] for g in got["groups"]}
    assert keys.get("tesco metro") == 3
    assert keys.get("somewhere else") == 1


def test_filing_a_merchant_files_every_row_of_it(live):
    cid = _ledger_with_unfiled(3)
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    _, _, r = call(live, "POST", "/api/edit/by-payee",
                   {"payee_key": "tesco metro", "category_id": cid}, headers=h)
    assert r["filed"] == 3

    from dashboard import storage as st
    conn = st.connect()
    left = conn.execute("SELECT COUNT(*) FROM transactions"
                        " WHERE category_id IS NULL").fetchone()[0]
    conn.close()
    assert left == 1, "filed the wrong rows too"


def test_filing_never_overwrites_a_decision_already_made(live):
    """This clears a backlog. It must not silently rewrite what you have
    already filed by hand."""
    from dashboard import ledger
    from dashboard import storage as st
    cid = _ledger_with_unfiled(2)
    conn = st.connect()
    gid = conn.execute("SELECT id FROM category_groups LIMIT 1").fetchone()[0]
    other = ledger.create_category(conn, gid, "Fuel")
    first = conn.execute(
        "SELECT id FROM transactions ORDER BY date LIMIT 1").fetchone()[0]
    ledger.update_transaction(conn, first, category_id=other)
    conn.close()

    cookie, csrf = login(live)
    call(live, "POST", "/api/edit/by-payee",
         {"payee_key": "tesco metro", "category_id": cid},
         headers={"Cookie": cookie, "X-CSRF-Token": csrf})

    conn = st.connect()
    kept = conn.execute("SELECT category_id FROM transactions WHERE id=?",
                        (first,)).fetchone()[0]
    conn.close()
    assert kept == other, "an existing category was overwritten"


def test_a_single_row_can_be_filed(live):
    cid = _ledger_with_unfiled(1)
    from dashboard import storage as st
    conn = st.connect()
    txn = conn.execute("SELECT id FROM transactions LIMIT 1").fetchone()[0]
    conn.close()
    cookie, csrf = login(live)
    status, _, _ = call(live, "PATCH", f"/api/edit/transaction/{txn}",
                        {"category_id": cid},
                        headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 200


def test_filing_into_a_category_that_does_not_exist_is_refused(live):
    _ledger_with_unfiled(1)
    cookie, csrf = login(live)
    status, _, _ = call(live, "POST", "/api/edit/by-payee",
                        {"payee_key": "tesco metro", "category_id": "0" * 32},
                        headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 404


def test_filed_merchants_can_be_listed_and_changed(live):
    """A wrong decision should be changeable, not lived with."""
    from dashboard import ledger
    from dashboard import storage as st
    cid = _ledger_with_unfiled(2)
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    call(live, "POST", "/api/edit/by-payee",
         {"payee_key": "tesco metro", "category_id": cid}, headers=h)

    _, _, unfiled = call(live, "GET", "/api/view/unfiled",
                         headers={"Cookie": cookie})
    assert "tesco metro" not in {g["key"] for g in unfiled["groups"]}

    _, _, done = call(live, "GET", "/api/view/unfiled?filed=1",
                      headers={"Cookie": cookie})
    grp = next(g for g in done["groups"] if g["key"] == "tesco metro")
    assert grp["category"] == "Groceries" and grp["count"] == 2

    conn = st.connect()
    gid = conn.execute("SELECT id FROM category_groups LIMIT 1").fetchone()[0]
    other = ledger.create_category(conn, gid, "Fuel")
    conn.close()

    _, _, r = call(live, "POST", "/api/edit/by-payee",
                   {"payee_key": "tesco metro", "category_id": other,
                    "overwrite": True}, headers=h)
    assert r["filed"] == 2

    _, _, again = call(live, "GET", "/api/view/unfiled?filed=1",
                       headers={"Cookie": cookie})
    grp = next(g for g in again["groups"] if g["key"] == "tesco metro")
    assert grp["category"] == "Fuel"


def test_incoming_and_outgoing_are_distinguishable(live):
    """The panel defaults to money in, because nothing arriving counts towards
    a budget until it is filed as income."""
    from dashboard import ledger
    from dashboard import storage as st
    conn = st.connect()
    acct = ledger.create_account(conn, "Checking")
    ledger.add_transaction(conn, acct, "2026-08-01", 250000, "PAYROLL DEPOSIT")
    ledger.add_transaction(conn, acct, "2026-08-02", -1200, "TESCO METRO 4471")
    conn.close()
    cookie, _ = login(live)
    _, _, got = call(live, "GET", "/api/view/unfiled", headers={"Cookie": cookie})
    by = {g["key"]: g["total_cents"] for g in got["groups"]}
    assert by["payroll deposit"] > 0
    assert by["tesco metro"] < 0


# ── splitting one payment ─────────────────────────────────────────────────
def _split_fixture():
    from dashboard import ledger
    from dashboard import storage as st
    conn = st.connect()
    g = ledger.create_category_group(conn, "Everyday")
    fuel = ledger.create_category(conn, g, "Gas")
    snacks = ledger.create_category(conn, g, "Snacks")
    acct = ledger.create_account(conn, "Checking")
    txn = ledger.add_transaction(conn, acct, "2026-08-01", -5000,
                                 "BUC-EES 4471", fuel)
    conn.close()
    return txn, fuel, snacks


def test_a_payment_splits_over_http(live):
    """Fuel and a sandwich from the same pump: one payment, two things."""
    txn, fuel, snacks = _split_fixture()
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    status, _, body = call(live, "POST", f"/api/edit/transaction/{txn}/split",
                           {"parts": [
                               {"category_id": fuel, "amount_cents": -4200},
                               {"category_id": snacks, "amount_cents": -800}]},
                           headers=h)
    assert status == 200
    assert sorted(p["amount_cents"] for p in body["parts"]) == [-4200, -800]


def test_parts_that_do_not_add_up_are_refused_over_http(live):
    txn, fuel, snacks = _split_fixture()
    cookie, csrf = login(live)
    status, _, body = call(live, "POST", f"/api/edit/transaction/{txn}/split",
                           {"parts": [
                               {"category_id": fuel, "amount_cents": -4200},
                               {"category_id": snacks, "amount_cents": -900}]},
                           headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 400 and "add up" in str(body)


def test_a_split_can_be_undone(live):
    txn, fuel, snacks = _split_fixture()
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    call(live, "POST", f"/api/edit/transaction/{txn}/split",
         {"parts": [{"category_id": fuel, "amount_cents": -4200},
                    {"category_id": snacks, "amount_cents": -800}]}, headers=h)
    _, _, r = call(live, "DELETE", f"/api/edit/transaction/{txn}/split",
                   headers=h)
    assert r["removed"] == 2
    _, _, after = call(live, "GET", f"/api/edit/transaction/{txn}/split",
                       headers={"Cookie": cookie})
    assert after["parts"] == []


def test_the_transaction_list_says_which_rows_can_be_split(live):
    """A split child cannot be divided again, so the control must not be
    offered on one -- a button whose only outcome is an error."""
    txn, fuel, snacks = _split_fixture()
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    call(live, "POST", f"/api/edit/transaction/{txn}/split",
         {"parts": [{"category_id": fuel, "amount_cents": -4200},
                    {"category_id": snacks, "amount_cents": -800}]}, headers=h)
    _, _, got = call(live, "GET", f"/api/view/transactions?category={fuel}",
                     headers={"Cookie": cookie})
    row = got["transactions"][0]
    assert row["parent_id"] == txn, "the list shows children, which cannot split"
    assert "category_id" in row


# ── income categories ─────────────────────────────────────────────────────
def test_a_category_can_be_created_as_income(live):
    """Without this there was no way to make one from the interface at all,
    so a wage filed as spending left the budget believing nothing arrived."""
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    _, _, grp = call(live, "POST", "/api/category-groups",
                     {"name": "Money"}, headers=h)
    _, _, cat = call(live, "POST", "/api/categories",
                     {"name": "Paycheck", "group_id": grp["id"],
                      "is_income": True}, headers=h)
    from dashboard import storage as st
    conn = st.connect()
    got = conn.execute("SELECT is_income FROM categories WHERE id=?",
                       (cat["id"],)).fetchone()[0]
    conn.close()
    assert got == 1


def test_a_category_in_an_income_group_is_income_by_default(live):
    """Putting Salary under Income and having it count as spending is not a
    distinction anyone means to draw."""
    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    _, _, grp = call(live, "POST", "/api/category-groups",
                     {"name": "Income", "is_income": True}, headers=h)
    _, _, cat = call(live, "POST", "/api/categories",
                     {"name": "Salary", "group_id": grp["id"]}, headers=h)
    from dashboard import storage as st
    conn = st.connect()
    got = conn.execute("SELECT is_income FROM categories WHERE id=?",
                       (cat["id"],)).fetchone()[0]
    conn.close()
    assert got == 1


def test_an_existing_category_can_be_switched_to_income(live):
    """The repair path: a wage already filed as spending has to be fixable
    without rebuilding the category and re-filing every row."""
    from dashboard import ledger
    from dashboard import storage as st
    conn = st.connect()
    gid = ledger.create_category_group(conn, "Money")
    cid = ledger.create_category(conn, gid, "Paycheck")
    acct = ledger.create_account(conn, "Checking")
    ledger.add_transaction(conn, acct, "2026-08-01", 250000, "PAYROLL", cid)
    conn.close()

    conn = st.connect()
    assert ledger.to_be_budgeted(conn, "2026-08") == 0, "counted before the fix"
    conn.close()

    cookie, csrf = login(live)
    call(live, "PATCH", f"/api/categories/{cid}", {"is_income": True},
         headers={"Cookie": cookie, "X-CSRF-Token": csrf})

    conn = st.connect()
    assert ledger.to_be_budgeted(conn, "2026-08") == 250000
    conn.close()


def test_switching_does_not_disturb_the_transactions(live):
    from dashboard import ledger
    from dashboard import storage as st
    conn = st.connect()
    gid = ledger.create_category_group(conn, "Money")
    cid = ledger.create_category(conn, gid, "Paycheck")
    acct = ledger.create_account(conn, "Checking")
    for i in range(3):
        ledger.add_transaction(conn, acct, "2026-08-0%d" % (i + 1), 100000,
                               "PAYROLL", cid)
    conn.close()
    cookie, csrf = login(live)
    call(live, "PATCH", f"/api/categories/{cid}", {"is_income": True},
         headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    conn = st.connect()
    n = conn.execute("SELECT COUNT(*) FROM transactions WHERE category_id=?",
                     (cid,)).fetchone()[0]
    conn.close()
    assert n == 3


def test_the_budget_lists_income_separately(live):
    """It is not budgeted into an envelope, but a category that vanishes on
    being marked income looks deleted and its money looks lost."""
    from dashboard import ledger
    from dashboard import storage as st
    conn = st.connect()
    ginc = ledger.create_category_group(conn, "Money", is_income=True)
    gexp = ledger.create_category_group(conn, "Everyday")
    pay = ledger.create_category(conn, ginc, "Paycheck", is_income=True)
    ledger.create_category(conn, gexp, "Groceries")
    acct = ledger.create_account(conn, "Checking")
    ledger.add_transaction(conn, acct, "2026-08-05", 250000, "PAYROLL", pay)
    conn.close()

    cookie, _ = login(live)
    status, _, b = call(live, "GET", "/api/view/budget?month=2026-08",
                        headers={"Cookie": cookie})
    assert status == 200, b
    assert [c["name"] for c in b["categories"]] == ["Groceries"], \
        "income should not have an envelope"
    assert [c["name"] for c in b["income"]] == ["Paycheck"]
    assert b["income"][0]["activity_cents"] == 250000
    assert b["to_be_budgeted_cents"] == 250000


# ── transfers ─────────────────────────────────────────────────────────────
def test_a_movement_can_be_recorded_against_an_untracked_account(live):
    """One statement imported means no second row to link to. The matching
    half is written where the money went, so it leaves the budget without
    being counted as spending."""
    from dashboard import ledger
    from dashboard import storage as st
    conn = st.connect()
    chk = ledger.create_account(conn, "Checking")
    venmo = ledger.create_account(conn, "Venmo", on_budget=False)
    for i in range(3):
        ledger.add_transaction(conn, chk, "2026-08-0%d" % (i + 1), -21100,
                               "Transfer to Venmo 4471")
    conn.close()

    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    _, _, avail = call(live, "GET", "/api/view/unfiled",
                       headers={"Cookie": cookie})
    assert [a["name"] for a in avail["tracking"]] == ["Venmo"], \
        "budgeted accounts must not be offered as transfer destinations"

    _, _, r = call(live, "POST", "/api/edit/by-payee",
                   {"payee_key": "transfer to venmo", "account_id": venmo},
                   headers=h)
    assert r["filed"] == 3 and r["as"] == "transfer"

    _, _, t = call(live, "GET", "/api/view/transfers", headers={"Cookie": cookie})
    assert len(t["transfers"]) == 3
    assert t["transfers"][0]["to_account"] == "Venmo"
    assert t["transfers"][0]["leaves_budget"] is True


def test_a_transfer_is_not_counted_as_spending(live):
    from dashboard import ledger
    from dashboard import storage as st
    conn = st.connect()
    chk = ledger.create_account(conn, "Checking")
    venmo = ledger.create_account(conn, "Venmo", on_budget=False)
    g = ledger.create_category_group(conn, "Everyday")
    cat = ledger.create_category(conn, g, "Groceries")
    txn = ledger.add_transaction(conn, chk, "2026-08-01", -21100, "Venmo", cat)
    conn.close()

    cookie, csrf = login(live)
    call(live, "POST", "/api/edit/transfer", {"id": txn, "account_id": venmo},
         headers={"Cookie": cookie, "X-CSRF-Token": csrf})

    conn = st.connect()
    # The category is gone, so the envelope no longer thinks it was spent.
    assert ledger.category_activity(conn, cat, "2026-08") == 0
    conn.close()


def test_candidate_pairs_are_offered_but_not_applied(live):
    from dashboard import ledger
    from dashboard import storage as st
    conn = st.connect()
    a = ledger.create_account(conn, "Checking")
    b = ledger.create_account(conn, "Savings")
    ledger.add_transaction(conn, a, "2026-08-01", -50000, "To savings")
    ledger.add_transaction(conn, b, "2026-08-02", 50000, "From checking")
    conn.close()

    cookie, csrf = login(live)
    _, _, t = call(live, "GET", "/api/view/transfers", headers={"Cookie": cookie})
    assert len(t["candidates"]) == 1 and t["transfers"] == []

    c = t["candidates"][0]
    call(live, "POST", "/api/edit/transfer",
         {"out_id": c["out_id"], "in_id": c["in_id"]},
         headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    _, _, t = call(live, "GET", "/api/view/transfers", headers={"Cookie": cookie})
    assert len(t["transfers"]) == 1 and t["candidates"] == []
    assert t["transfers"][0]["from_account"] == "Checking"
    assert t["transfers"][0]["to_account"] == "Savings"


def test_a_transfer_can_be_unlinked_over_http(live):
    """DELETE with a body: the id identifies which pair to separate."""
    from dashboard import ledger
    from dashboard import storage as st
    conn = st.connect()
    a = ledger.create_account(conn, "Checking")
    b = ledger.create_account(conn, "Savings")
    out = ledger.add_transaction(conn, a, "2026-08-01", -50000, "To savings")
    inn = ledger.add_transaction(conn, b, "2026-08-01", 50000, "From checking")
    ledger.link_transfer(conn, out, inn)
    conn.close()

    cookie, csrf = login(live)
    h = {"Cookie": cookie, "X-CSRF-Token": csrf}
    _, _, before = call(live, "GET", "/api/view/transfers",
                        headers={"Cookie": cookie})
    assert len(before["transfers"]) == 1

    status, _, r = call(live, "DELETE", "/api/edit/transfer", {"id": out},
                        headers=h)
    assert status == 200 and len(r["unlinked"]) == 2

    _, _, after = call(live, "GET", "/api/view/transfers",
                       headers={"Cookie": cookie})
    assert after["transfers"] == []
    # Both rows survive: unlinking separates, it does not delete.
    conn = st.connect()
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2
    conn.close()


# ── where things are filed ────────────────────────────────────────────────
def test_the_ledger_tree_nests_account_group_category(live):
    """The order is the order in which a thing turns out to be wrong."""
    from dashboard import ledger
    from dashboard import storage as st
    conn = st.connect()
    acct = ledger.create_account(conn, "Checking")
    g = ledger.create_category_group(conn, "Everyday")
    cat = ledger.create_category(conn, g, "Groceries")
    ledger.add_transaction(conn, acct, "2026-08-01", -1200, "Tesco", cat)
    ledger.add_transaction(conn, acct, "2026-08-02", -800, "Unfiled thing")
    conn.close()

    cookie, _ = login(live)
    _, _, d = call(live, "GET", "/api/view/ledger?month=2026-08",
                   headers={"Cookie": cookie})
    tree = d["tree"]
    assert [a["name"] for a in tree] == ["Checking"]
    names = {g["name"] for g in tree[0]["groups"]}
    assert "Everyday" in names
    # Uncategorised rows still appear: hiding them would make the screen
    # disagree with the ledger about what exists.
    assert "No category" in names


def test_a_row_can_be_refiled_and_moves_in_the_tree(live):
    from dashboard import ledger
    from dashboard import storage as st
    conn = st.connect()
    acct = ledger.create_account(conn, "Checking")
    g = ledger.create_category_group(conn, "Everyday")
    a = ledger.create_category(conn, g, "Groceries")
    b = ledger.create_category(conn, g, "Fuel")
    txn = ledger.add_transaction(conn, acct, "2026-08-01", -1200, "Shell", a)
    conn.close()

    cookie, csrf = login(live)
    call(live, "PATCH", f"/api/edit/transaction/{txn}", {"category_id": b},
         headers={"Cookie": cookie, "X-CSRF-Token": csrf})

    _, _, d = call(live, "GET", "/api/view/ledger?month=2026-08",
                   headers={"Cookie": cookie})
    cats = {c["name"]: c["rows"] for g_ in d["tree"][0]["groups"]
            for c in g_["categories"]}
    assert len(cats.get("Fuel", [])) == 1
    assert not cats.get("Groceries")


def test_a_single_transaction_can_be_deleted(live):
    from dashboard import ledger
    from dashboard import storage as st
    conn = st.connect()
    acct = ledger.create_account(conn, "Checking")
    txn = ledger.add_transaction(conn, acct, "2026-08-01", -1200, "Wrong row")
    conn.close()
    cookie, csrf = login(live)
    status, _, r = call(live, "DELETE", f"/api/edit/transaction/{txn}",
                        headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 200 and r["removed"] == 1
    conn = st.connect()
    assert ledger.account_balance(conn, acct) == 0
    conn.close()


def test_deleting_something_that_is_not_there(live):
    cookie, csrf = login(live)
    status, _, _ = call(live, "DELETE", "/api/edit/transaction/" + "0" * 32,
                        headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 404


def test_the_ledger_tree_can_span_every_month(live):
    """The month control offers "every month", so the endpoint has to answer
    without one -- a search for a charge you cannot date is the whole reason."""
    from dashboard import ledger
    from dashboard import storage as st
    conn = st.connect()
    acct = ledger.create_account(conn, "Checking")
    ledger.add_transaction(conn, acct, "2026-06-01", -100, "June thing")
    ledger.add_transaction(conn, acct, "2026-09-01", -200, "September thing")
    conn.close()

    cookie, _ = login(live)
    _, _, one = call(live, "GET", "/api/view/ledger?month=2026-09",
                     headers={"Cookie": cookie})
    _, _, all_ = call(live, "GET", "/api/view/ledger",
                      headers={"Cookie": cookie})

    def count(d):
        return sum(len(c["rows"]) for a in d["tree"]
                   for g in a["groups"] for c in g["categories"])
    assert count(one) == 1 and count(all_) == 2
    assert all_["month"] is None


def test_the_batch_list_says_which_account_each_went_to(live):
    """The import screen defaults its account picker to whatever was imported
    last, and a name cannot select an option."""
    from dashboard import ledger
    from dashboard import statements as st
    from dashboard import storage as store
    conn = store.connect()
    acct = ledger.create_account(conn, "Everyday Checking")
    conn.close()
    cookie, _ = login(live)
    conn = store.connect()
    bid, _ = st.create_batch(conn, acct, "aug.csv",
                             b"date,description,amount\n2026-08-01,Tesco,-12.00\n")
    st.commit_batch(conn, bid)
    conn.close()

    _, _, d = call(live, "GET", "/api/import/batches", headers={"Cookie": cookie})
    b = d["batches"][0]
    assert b["account_id"] == acct
    assert b["account"] == "Everyday Checking"


# ── the savings plan ──────────────────────────────────────────────────────
def _seed_savings(port):
    """Four covered months of one category, so a plan can be built."""
    from dashboard import ledger, stats, storage
    conn = storage.connect()
    acct = ledger.create_account(conn, "Checking")
    grp = ledger.create_category_group(conn, "Everyday")
    cat = ledger.create_category(conn, grp, "Eating Out")
    ms = stats.month_range("2025-08", 4)
    for m, amount in zip(ms, (100_00, 200_00, 300_00, 400_00), strict=True):
        conn.execute(
            "INSERT OR REPLACE INTO month_coverage (account_id, month,"
            " txn_count, first_day, last_day) VALUES (?,?,?,?,?)",
            (acct, m, 1, m + "-01", m + "-28"))
        ledger.add_transaction(conn, acct, "%s-10" % m, -amount, "Cafe", cat)
    conn.commit()
    conn.close()
    return {"acct": acct, "cat": cat}


def test_changing_a_flexibility_needs_csrf(live):
    cookie, _ = login(live)
    ids = _seed_savings(live)
    status, _, _ = call(live, "PATCH", f"/api/savings/flexibility/{ids['cat']}",
                        {"flexibility": "essential"}, headers={"Cookie": cookie})
    assert status == 403


def test_marking_a_category_essential_stops_it_being_cut(live):
    """The one thing flexibility exists to guarantee.

    You cannot decide to pay less rent, so a category you have marked
    essential must be budgeted at what it will cost -- never trimmed below
    its own forecast.
    """
    cookie, csrf = login(live)
    ids = _seed_savings(live)
    hdrs = {"Cookie": cookie, "X-CSRF-Token": csrf}

    path = f"/api/view/history?month=2025-08&category={ids['cat']}&months=12"
    _, _, before = call(live, "GET", path, headers={"Cookie": cookie})
    assert before["suggested_cents"] < before["estimate_cents"], "trimmed by default"

    status, _, _ = call(live, "PATCH", f"/api/savings/flexibility/{ids['cat']}",
                        {"flexibility": "essential"}, headers=hdrs)
    assert status == 200

    _, _, after = call(live, "GET", path, headers={"Cookie": cookie})
    assert after["flexibility"] == "essential"
    assert after["suggested_cents"] == after["estimate_cents"]
    assert after["target_reason"] == "fixed cost, budget what it will be"


def test_an_invented_flexibility_is_a_400(live):
    cookie, csrf = login(live)
    ids = _seed_savings(live)
    status, _, _ = call(live, "PATCH", f"/api/savings/flexibility/{ids['cat']}",
                        {"flexibility": "free"},
                        headers={"Cookie": cookie, "X-CSRF-Token": csrf})
    assert status == 400


# ── one charge, in full ───────────────────────────────────────────────────
def test_a_charge_detail_needs_a_session(live):
    status, _, _ = call(live, "GET", "/api/view/transaction/" + "a" * 32)
    assert status == 401


def test_a_charge_detail_carries_the_banks_fuller_description(live):
    """The list shows NAME, which the bank truncates. MEMO is the answer to
    "what actually was this", and was stored and never shown."""
    cookie, _ = login(live)
    from dashboard import ledger, storage
    conn = storage.connect()
    acct = ledger.create_account(conn, "Checking")
    grp = ledger.create_category_group(conn, "Everyday")
    cat = ledger.create_category(conn, grp, "Work Food")
    tid = ledger.add_transaction(
        conn, acct, "2026-08-26", -13_72, "Riverside Cafe - Br Bristol Uk W",
        cat, notes="SQ *RIVERSIDE CAFE - BR Bristol UK")
    conn.close()

    status, _, body = call(live, "GET", f"/api/view/transaction/{tid}",
                           headers={"Cookie": cookie})
    assert status == 200, body
    assert body["payee"] == "Riverside Cafe - Br Bristol Uk W"
    assert body["description"] == "SQ *RIVERSIDE CAFE - BR Bristol UK"
    assert body["category"] == "Work Food"
    assert body["account"] == "Checking"
    assert body["amount_cents"] == -13_72


def test_an_unknown_charge_is_a_404_not_a_crash(live):
    cookie, _ = login(live)
    status, _, _ = call(live, "GET", "/api/view/transaction/" + "b" * 32,
                        headers={"Cookie": cookie})
    assert status == 404


def test_a_charge_detail_names_the_other_half_of_a_transfer(live):
    cookie, _ = login(live)
    from dashboard import ledger, storage
    conn = storage.connect()
    a = ledger.create_account(conn, "Checking")
    b = ledger.create_account(conn, "Savings")
    out = ledger.add_transaction(conn, a, "2026-01-02", -50_00, "To savings")
    inn = ledger.add_transaction(conn, b, "2026-01-03", 50_00, "From checking")
    ledger.link_transfer(conn, out, inn)
    conn.close()

    _, _, body = call(live, "GET", f"/api/view/transaction/{out}",
                      headers={"Cookie": cookie})
    assert body["is_transfer"] is True
    assert body["transfer_with"]["account"] == "Savings"
    assert body["transfer_with"]["amount_cents"] == 50_00


# ── the budget widget's chart picks what it draws ─────────────────────────
def _seed_widget(port):
    """Categories where the biggest spender is not the fullest envelope."""
    from dashboard import ledger, stats, storage
    conn = storage.connect()
    month = stats.this_month()
    acct = ledger.create_account(conn, "Checking")
    grp = ledger.create_category_group(conn, "Everyday")
    made = {}
    for name, budget, spent in (
            ("Rent", 100_000, 20_000),        # huge, barely touched: 20%
            ("Subscriptions", 1_000, 990),    # tiny, nearly gone:    99%
            ("Groceries", 50_000, 25_000),    # big, halfway:         50%
            ("Parking", 500, 450)):           # tiny, nearly gone:    90%
        cid = ledger.create_category(conn, grp, name)
        made[name] = cid
        ledger.set_budget(conn, month, cid, budget)
        if spent:
            ledger.add_transaction(conn, acct, f"{month}-05", -spent, name, cid)
    conn.close()
    return made


def test_the_widget_ranks_by_how_full_the_envelope_is(live):
    """The chart draws a proportion, so the selection must be a proportion.

    Ranking by amount spent picks the largest categories, which is a
    different question -- and with only seven slots it is how a category at
    99% gets crowded out by seven big ones at 20%.
    """
    cookie, _ = login(live)
    _seed_widget(live)
    _, _, body = call(live, "GET", "/api/view/home", headers={"Cookie": cookie})
    names = [c["name"] for c in body["budget"]["top"]]
    assert names[:4] == ["Subscriptions", "Parking", "Groceries", "Rent"]


def test_a_category_with_no_budget_sorts_after_those_with_one(live):
    """It has no proportion to be near, but its spending is real -- so it
    goes last rather than being dropped."""
    cookie, _ = login(live)
    from dashboard import ledger, stats, storage
    made = _seed_widget(live)
    conn = storage.connect()
    month = stats.this_month()
    grp = conn.execute("SELECT group_id FROM categories WHERE id=?",
                       (made["Rent"],)).fetchone()["group_id"]
    loose = ledger.create_category(conn, grp, "Unbudgeted")
    acct = conn.execute("SELECT id FROM accounts LIMIT 1").fetchone()["id"]
    ledger.add_transaction(conn, acct, f"{month}-06", -9_999, "Whatever", loose)
    conn.close()

    _, _, body = call(live, "GET", "/api/view/home", headers={"Cookie": cookie})
    names = [c["name"] for c in body["budget"]["top"]]
    assert "Unbudgeted" in names, "spending must not vanish"
    assert names.index("Unbudgeted") > names.index("Rent")


def test_an_untouched_envelope_ranks_last_among_budgeted(live):
    cookie, _ = login(live)
    from dashboard import ledger, stats, storage
    _seed_widget(live)
    conn = storage.connect()
    month = stats.this_month()
    grp = conn.execute("SELECT id FROM category_groups LIMIT 1").fetchone()["id"]
    idle = ledger.create_category(conn, grp, "Untouched")
    ledger.set_budget(conn, month, idle, 20_000)
    conn.close()

    _, _, body = call(live, "GET", "/api/view/home", headers={"Cookie": cookie})
    names = [c["name"] for c in body["budget"]["top"]]
    assert names[-1] == "Untouched"
