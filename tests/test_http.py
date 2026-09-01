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

    # A later sync re-asserts the classifier's low score; the correction wins.
    _, _, again = call(live, "POST", "/api/ingest/messages", {
        "account": acct,
        "messages": [{"source_uid": "m1", "subject": "Invoice",
                      "received_at": "2026-09-01T09:00:00", "importance": 1}],
    }, headers=h_ing)
    assert again["skipped_local_edits"] == 1

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
