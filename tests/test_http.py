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


# ── Google sign-in routes ─────────────────────────────────────────────────
def test_config_reports_google_off_when_unconfigured(live, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    status, _, body = call(live, "GET", "/api/config")
    assert status == 200 and body["google_enabled"] is False


def test_config_never_leaks_the_client_id(live, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "shh")
    _, _, body = call(live, "GET", "/api/config")
    assert body == {"google_enabled": True}, "config should say only whether, not what"


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
