"""Reading calendar and mail from Google.

The important architectural point lives here rather than in n8n: this process
is the only thing that ever holds a Google credential. A workflow engine asks
this application to sync; it does not get a token of its own and does not talk
to Google. That keeps the refresh token -- the long-lived secret, the one that
is worth stealing -- inside one process with one encrypted store, instead of
copied into a second system whose own encryption key sits in a plaintext file.

Everything here is standard library. The Google client libraries are large,
pull a dependency tree behind them, and buy nothing that four REST calls and
urllib do not already do.
"""
import datetime as _dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import auth_google, crypt, settings

CAL_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
GMAIL_LIST = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
GMAIL_GET = "https://gmail.googleapis.com/gmail/v1/users/me/messages/%s"

HTTP_TIMEOUT = 20
# A cap, not a target. A scheduled sync that quietly grows into a thousand
# requests is how an API quota is exhausted at three in the morning.
MAX_MESSAGES = 40
MAX_EVENTS = 250
REFRESH_MARGIN = 120        # refresh this many seconds before expiry


class GoogleError(Exception):
    pass


# ── access tokens ─────────────────────────────────────────────────────────
def _expired(expires_at):
    if not expires_at:
        return True
    try:
        return float(expires_at) - time.time() < REFRESH_MARGIN
    except (TypeError, ValueError):
        return True


def access_token(conn, account_id, force=False):
    """A live access token for one account, refreshed only when needed.

    Refreshing on every call would work and would also mean a token request to
    Google for every sync tick, which is both slower and a good way to meet a
    rate limit. The stored token is reused until it is nearly expired.
    """
    row = conn.execute(
        "SELECT access_token, expires_at FROM google_accounts WHERE id=?",
        (account_id,)).fetchone()
    if row is None:
        raise GoogleError("no such account")

    if not force and not _expired(row["expires_at"]) and row["access_token"]:
        try:
            return crypt.decrypt(row["access_token"])
        except Exception:
            pass        # unreadable cache is not fatal; fall through to refresh

    refresh = settings.google_refresh_token(conn, account_id)
    if not refresh:
        raise GoogleError("account has no refresh token -- reconnect it")
    try:
        tok = auth_google.refresh(refresh, conn)
    except auth_google.OAuthError as e:
        raise GoogleError(str(e)) from None

    fresh = tok.get("access_token")
    if not fresh:
        raise GoogleError("Google returned no access token")
    expires = time.time() + float(tok.get("expires_in") or 3600)
    conn.execute("UPDATE google_accounts SET access_token=?, expires_at=?"
                 " WHERE id=?", (crypt.encrypt(fresh), expires, account_id))
    conn.commit()
    return fresh


def _get(url, token, params=None):
    if params:
        # doseq, because Gmail wants metadataHeaders repeated rather than sent
        # once. Without it urlencode serialises the list's repr as a single
        # value, Gmail ignores the parameter and returns its own default set
        # of headers -- which includes From, Subject and Date, so the call
        # appears to work right up until you rely on a header outside that
        # set and find it silently absent.
        url = url + "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + token,
                      "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise _Unauthorized() from None
        # Google's `reason` is a fixed enum, so it is safe to read and worth
        # translating. The human-readable `message` beside it is free text
        # that echoes the request back, so it is never surfaced.
        reason = ""
        try:
            err = json.loads(e.read().decode()).get("error", {})
            details = err.get("errors") or [{}]
            reason = str(details[0].get("reason") or err.get("status") or "")[:60]
        except Exception:
            pass
        raise GoogleError(_explain(reason, url, e.code)) from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise GoogleError("could not reach Google: %s" % e.reason) from None


def _api_name(url):
    return "Calendar" if url.startswith(CAL_URL) else "Gmail"


def _enable_link(url):
    return ("https://console.cloud.google.com/apis/library/"
            + ("calendar-json.googleapis.com" if url.startswith(CAL_URL)
               else "gmail.googleapis.com"))


def _explain(reason, url, code):
    """Turn Google's error code into the thing you actually have to go do.

    A bare 403 is the single most common wall when first connecting a new
    Cloud project, and it means the API was never switched on -- which the
    status code does not hint at even slightly.
    """
    api = _api_name(url)
    if reason in ("accessNotConfigured", "SERVICE_DISABLED"):
        return ("The %s API is not enabled in your Google Cloud project. "
                "Enable it at %s, wait a minute, then sync again."
                % (api, _enable_link(url)))
    if reason in ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
                  "forbidden"):
        return ("This account has not granted the permissions the %s API "
                "needs. Reconnect it in Settings and accept both requests."
                % api)
    if reason in ("rateLimitExceeded", "userRateLimitExceeded",
                  "quotaExceeded", "RESOURCE_EXHAUSTED"):
        return ("Google is rate limiting the %s API. This usually clears on "
                "its own; the next sync will retry." % api)
    if reason:
        return "%s API refused the request (%s, HTTP %s)" % (api, reason, code)
    return "%s API error (HTTP %s)" % (api, code)


class _Unauthorized(Exception):
    pass


def _get_retrying(conn, account_id, url, params=None):
    """One 401 is a stale token, not a failure. Two is a real problem."""
    token = access_token(conn, account_id)
    try:
        return _get(url, token, params)
    except _Unauthorized:
        token = access_token(conn, account_id, force=True)
    try:
        return _get(url, token, params)
    except _Unauthorized:
        raise GoogleError("Google rejected the refreshed token -- "
                          "the account may need reconnecting") from None


# ── time ──────────────────────────────────────────────────────────────────
def _to_utc(value):
    """RFC 3339 with any offset -> naive ISO 8601 in UTC.

    The database stores UTC throughout. Mixing offsets into that column would
    make ordering by starts_at silently wrong for anyone who travels.
    """
    if not value:
        return ""
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = _dt.datetime.fromisoformat(text)
    except ValueError:
        return text[:19]
    if dt.tzinfo is not None:
        dt = dt.astimezone(_dt.UTC).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


# ── calendar ──────────────────────────────────────────────────────────────
def fetch_events(conn, account_id, days_back=1, days_ahead=21):
    """Events in a window around now, recurrences already expanded.

    singleEvents asks Google to do the expansion. Doing it here would mean
    reimplementing RRULE, which is a great deal of subtle work to arrive at a
    worse answer.
    """
    now = _dt.datetime.now(_dt.UTC)
    params = {
        "timeMin": (now - _dt.timedelta(days=days_back)).isoformat(),
        "timeMax": (now + _dt.timedelta(days=days_ahead)).isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": 250,
        "showDeleted": "true",
    }
    out, page = [], None
    while len(out) < MAX_EVENTS:
        if page:
            params["pageToken"] = page
        data = _get_retrying(conn, account_id, CAL_URL, params)
        for item in data.get("items", []):
            start, end = item.get("start") or {}, item.get("end") or {}
            all_day = "date" in start
            starts = _to_utc(start.get("dateTime") or start.get("date"))
            if not starts:
                continue
            out.append({
                "source_uid": item.get("id", ""),
                "calendar_id": "primary",
                "title": item.get("summary", "") or "(no title)",
                "description": item.get("description", "") or "",
                "location": item.get("location", "") or "",
                "starts_at": starts,
                "ends_at": _to_utc(end.get("dateTime") or end.get("date")),
                "all_day": 1 if all_day else 0,
                "status": item.get("status", "confirmed"),
                # A cancelled event must come through as deleted rather than
                # be dropped: dropping it leaves the old copy on the agenda
                # forever, which is worse than never having synced it.
                "deleted": 1 if item.get("status") == "cancelled" else 0,
            })
        page = data.get("nextPageToken")
        if not page:
            break
    return out[:MAX_EVENTS]


# ── mail ──────────────────────────────────────────────────────────────────
def _header(headers, name):
    lowered = name.lower()
    for h in headers:
        if str(h.get("name", "")).lower() == lowered:
            return h.get("value", "")
    return ""


def _split_from(value):
    """'Ada Lovelace <ada@example.com>' -> ('Ada Lovelace', 'ada@example.com')"""
    text = str(value or "").strip()
    if "<" in text and ">" in text:
        name = text.split("<", 1)[0].strip().strip('"')
        addr = text.split("<", 1)[1].split(">", 1)[0].strip()
        return name or addr, addr.lower()
    return text, text.lower()


def _baseline(labels, bulk=False, direct=False):
    """A first-pass importance from signals that cost nothing to read.

    Deliberately dull and explainable. It exists so the inbox is useful before
    any model has run, and so there is always a reason to show -- a score with
    no reason can only be trusted blindly or ignored.

    The two signals that matter most are not Gmail's own labels. Mail carrying
    List-Unsubscribe is, by its own admission, bulk: no human typed it to you.
    Mail addressed to you by name in To is the opposite. Without those, every
    unread message scores alike and the ranking says nothing -- which is how
    an inbox ends up showing twenty-six newsletters and one real message all
    at the same weight.
    """
    labels = set(labels or [])
    promo = "CATEGORY_PROMOTIONS" in labels or "CATEGORY_SOCIAL" in labels
    unread = "UNREAD" in labels

    if "STARRED" in labels:
        return 5, "you starred it"
    if "IMPORTANT" in labels and unread:
        return 4, "Gmail marked this important and it is unread"
    if "IMPORTANT" in labels:
        return 3, "Gmail marked this important"

    if bulk and promo:
        return 1, "a promotional mailing you can unsubscribe from"
    if bulk:
        return 2, "a mailing list or newsletter"
    if promo:
        return 2, "promotional or social mail"

    # Nothing says bulk, so a person plausibly sent this.
    if direct and unread:
        return 4, "addressed to you directly, and unread"
    if direct:
        return 3, "addressed to you directly"
    if unread:
        return 3, "unread, and not a mailing"
    return 2, "read, and not a mailing"


def fetch_messages(conn, account_id, query="-in:chats newer_than:14d",
                   max_results=MAX_MESSAGES):
    row = conn.execute("SELECT email FROM google_accounts WHERE id=?",
                       (account_id,)).fetchone()
    mine = (row["email"] or "").lower() if row else ""
    listing = _get_retrying(conn, account_id, GMAIL_LIST, {
        "maxResults": min(int(max_results), MAX_MESSAGES),
        "q": query,
    })
    out = []
    for stub in listing.get("messages", []) or []:
        mid = stub.get("id")
        if not mid:
            continue
        try:
            full = _get_retrying(
                conn, account_id, GMAIL_GET % urllib.parse.quote(mid),
                {"format": "metadata",
                 "metadataHeaders": ["From", "Subject", "Date", "To",
                                     "List-Unsubscribe"]})
        except GoogleError:
            # One unreadable message must not lose the other thirty-nine.
            continue
        headers = (full.get("payload") or {}).get("headers", [])
        labels = full.get("labelIds", []) or []
        name, addr = _split_from(_header(headers, "From"))
        bulk = bool(_header(headers, "List-Unsubscribe"))
        direct = bool(mine) and mine in _header(headers, "To").lower()
        score, reason = _baseline(labels, bulk=bulk, direct=direct)
        received = ""
        if full.get("internalDate"):
            received = _dt.datetime.fromtimestamp(
                int(full["internalDate"]) / 1000,
                _dt.UTC).strftime("%Y-%m-%dT%H:%M:%S")
        if not received:
            received = _to_utc(_header(headers, "Date"))
        out.append({
            "source_uid": mid,
            "thread_id": full.get("threadId", ""),
            "sender": name,
            "sender_email": addr,
            "subject": _header(headers, "Subject") or "(no subject)",
            "snippet": full.get("snippet", "") or "",
            "received_at": received,
            "is_unread": 1 if "UNREAD" in labels else 0,
            "is_starred": 1 if "STARRED" in labels else 0,
            "archived": 0 if "INBOX" in labels else 1,
            "labels": ",".join(labels),
            "importance": score,
            "reason": reason,
        })
    return out
