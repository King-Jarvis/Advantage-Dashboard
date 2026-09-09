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
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import auth_google, crypt, mailparts, mailtext, settings, times

CAL_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
GMAIL_LIST = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
GMAIL_GET = "https://gmail.googleapis.com/gmail/v1/users/me/messages/%s"

HTTP_TIMEOUT = 20
# A cap, not a target. A scheduled sync that quietly grows into a thousand
# requests is how an API quota is exhausted at three in the morning.
MAX_MESSAGES = 40
# How far back the mail queries reach. Named because the purge has to know
# it: a message deleted while Gmail would still hand it back simply returns
# on the next sync.
MAIL_WINDOW_DAYS = 14
# Ids per page when sweeping read state. Ids only, so the page can be large.
READ_SWEEP_PAGE = 500
# A year forward and a season back. Narrower than this and the calendar can
# only show the month you are standing in, which is not a calendar.
DAYS_BACK = 90
DAYS_AHEAD = 365
MAX_EVENTS = 2500
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

    if not crypt.available():
        # Distinguishing these matters: one means reconnect the account, the
        # other means the encryption key is missing and reconnecting would
        # store a token that cannot be read back either. Reporting the first
        # when it is the second sends you round a loop that cannot succeed.
        raise GoogleError(
            "the encryption key is not readable, so the saved Google token "
            "cannot be decrypted. Check TOKEN_KEY_PATH.")
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
        if e.code == 410:
            raise GoneError() from None
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


def _reminder_of(item):
    """The first popup override, or -1 for the calendar's own default."""
    rem = item.get("reminders") or {}
    if rem.get("useDefault", True):
        return -1
    for o in rem.get("overrides") or []:
        try:
            return int(o.get("minutes"))
        except (TypeError, ValueError):
            continue
    return -1


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


class _Replay:
    """An HTTPError whose body has already been read.

    read() is one-shot on a socket, so once the 400 handler has looked for a
    restriction the later error-formatting code would find nothing and report
    a bare status. This hands it the bytes again.
    """

    def __init__(self, err, body):
        self._err, self._body = err, body
        self.code = err.code

    def read(self):
        return self._body


class Refused(Exception):
    """Google will never accept this, however many times it is retried.

    Distinct from GoogleError, which covers the transient and the fixable. A
    permanent refusal that retries is a queue that never drains and an edit
    that never resolves either way.
    """


class GoneError(Exception):
    """Google's sync token has expired. Means start again, not something
    went wrong."""


class Conflict(Exception):
    """Google's copy moved since we read it. The local edit is not lost --
    it stays dirty and is reported, rather than being pushed over the top."""


def _send(url, token, method="POST", payload=None, etag=None):
    """A write. Same error handling as _get, including the 401 signal."""
    data = json.dumps(payload).encode() if payload is not None else b""
    headers = {"Authorization": "Bearer " + token,
               "Accept": "application/json",
               "Content-Type": "application/json"}
    # An etag turns a blind overwrite into a conditional one: if Google's copy
    # moved since we read it, this fails with 412 instead of quietly
    # discarding whatever changed there.
    if etag:
        headers["If-Match"] = etag
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            body = r.read()
            return json.loads(body.decode()) if body.strip() else {}
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise _Unauthorized() from None
        if e.code == 412:
            raise Conflict() from None
        if e.code in (404, 410):
            # Whatever we were changing is not there any more. Retrying a
            # write against something that no longer exists cannot start
            # working. Only on a write: a 410 on a read is a stale sync
            # token, which does mean try again.
            raise Refused(
                "that is no longer in Google -- it was probably deleted "
                "there, so the change here could not be applied") from None
        if e.code == 400:
            body = b""
            try:
                body = e.read()
            except Exception:
                pass
            if b"eventTypeRestriction" in body:
                raise Refused(
                    "Google does not allow this kind of event to be changed "
                    "through an app. Birthdays it creates from your profile "
                    "or contacts are its own -- edit it in Google Calendar, "
                    "or make your own event on that date."
                ) from None
            e = _Replay(e, body)
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


def _send_retrying(conn, account_id, url, method="POST", payload=None,
                   etag=None):
    token = access_token(conn, account_id)
    try:
        return _send(url, token, method, payload, etag)
    except _Unauthorized:
        token = access_token(conn, account_id, force=True)
    try:
        return _send(url, token, method, payload, etag)
    except _Unauthorized:
        raise GoogleError("Google rejected the refreshed token -- "
                          "the account may need reconnecting") from None


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
# Kept in times.py: the event writer needs exactly the same conversion, and
# two copies is how two parts of one application disagree about when
# something happens.
_to_utc = times.to_utc


# ── calendar ──────────────────────────────────────────────────────────────
def fetch_events(conn, account_id, days_back=DAYS_BACK, days_ahead=DAYS_AHEAD,
                 cursor=None):
    """Events in a window around now, and the token to fetch changes next time.

    Returns (events, next_cursor).

    The first call asks for the whole window. Every call after that sends the
    sync token Google handed back and receives only what changed -- which is
    what makes a year-wide window affordable to poll every few minutes
    instead of re-downloading a thousand events to discover that none of them
    moved.

    A token expires, and Google says so with 410. That is not an error worth
    reporting: it means start again, so we do, once.
    """
    params = {"maxResults": 250, "showDeleted": "true"}
    if cursor:
        # timeMin and timeMax must not be sent with a sync token -- the window
        # is already baked into it by the request that created it.
        params["syncToken"] = cursor
    else:
        now = _dt.datetime.now(_dt.UTC)
        params.update({
            "timeMin": (now - _dt.timedelta(days=days_back)).isoformat(),
            "timeMax": (now + _dt.timedelta(days=days_ahead)).isoformat(),
            "singleEvents": "true",
            # No orderBy. Google withholds nextSyncToken whenever it is set,
            # which quietly turns every poll back into a full year-wide
            # download -- and it buys nothing here, because the database
            # sorts on read and the grid sorts again per day.
        })

    out, page, next_cursor = [], None, None
    while len(out) < MAX_EVENTS:
        if page:
            params["pageToken"] = page
        try:
            data = _get_retrying(conn, account_id, CAL_URL, params)
        except GoneError:
            if not cursor:
                raise
            # The token aged out. Start again from a full window, once.
            return fetch_events(conn, account_id, days_back, days_ahead, None)

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
                # be dropped: dropping it leaves the old copy on the calendar
                # for ever.
                "deleted": 1 if item.get("status") == "cancelled" else 0,
                "etag": str(item.get("etag") or ""),
                # An expanded instance does not carry its series' rule, only
                # the id of the series it belongs to. Inventing a rule here
                # would put a fabricated RRULE into the table and fail
                # validation the next time the event was edited.
                "recurrence": (item.get("recurrence") or [""])[0]
                              if item.get("recurrence") else "",
                "series_id": str(item.get("recurringEventId") or ""),
                "event_type": str(item.get("eventType") or "default"),
                "reminder_minutes": _reminder_of(item),
            })
        page = data.get("nextPageToken")
        if not page:
            # Only the final page carries it.
            next_cursor = data.get("nextSyncToken") or cursor
            break
    return out[:MAX_EVENTS], next_cursor


# ── mail ──────────────────────────────────────────────────────────────────
def fetch_message(conn, account_id, source_uid):
    """One message as (text, blocks), from a single request.

    Both forms come from the same payload: fetching twice would double the
    quota cost to produce two views of identical bytes.
    """
    full = _get_retrying(conn, account_id,
                         GMAIL_GET % urllib.parse.quote(source_uid),
                         {"format": "full"})
    payload = full.get("payload") or {}
    return mailtext.from_payload(payload), mailparts.blocks_from_payload(payload)


def fetch_body(conn, account_id, source_uid):
    """The readable text of one message, fetched in full.

    Deliberately one at a time and only when asked. Fetching format=full for
    every message during sync would multiply the request count by the size of
    the mailbox and buy nothing -- the list view never shows a body.
    """
    full = _get_retrying(conn, account_id, GMAIL_GET % urllib.parse.quote(source_uid),
                         {"format": "full"})
    return mailtext.from_payload(full.get("payload") or {})


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


MACHINE = re.compile(
    r"no[-_.]?reply|do[-_.]?not[-_.]?reply|auto[-_.]?(confirm|reply)|"
    r"notification|automated|mailer|bounce|postmaster|"
    r"(order|shipment|delivery)[-_.]?(update|tracking|confirm)",
    re.I)


def _is_machine(address):
    """Does the local part announce that nobody is reading replies?"""
    return bool(MACHINE.search(str(address or "").split("@")[0]))


# Mail telling you something happened to an account. Whole phrases, not the
# word "security" on its own -- "security tips", "your security is our
# priority" and half of every marketing footer contain that word, and a band
# that fills with marketing stops being read, which costs more than the alert
# it was protecting.
#
# A 2FA code counts. It is the most time-critical mail anyone receives: you
# are standing at a login screen waiting for it.
_SECURITY = re.compile(
    r"security alert"
    r"|securityalert"
    r"|critical (?:security|alert)"
    r"|new (?:sign[- ]?in|login|device)"
    r"|(?:unusual|suspicious|unrecognized|unrecognised) "
    r"(?:activity|sign[- ]?in|login|device|access)"
    r"|password (?:was|has been|is being) (?:changed|reset)"
    r"|(?:reset|change) your password"
    r"|verification code"
    r"|(?:verify|confirm) your identity"
    r"|two[- ]?(?:factor|step)|2[- ]?(?:factor|step)"
    r"|account (?:was|has been) (?:locked|suspended|compromised)"
    r"|someone (?:has |)(?:signed|logged) in",
    re.I)


def _is_security(subject):
    """Does this subject say something happened to an account?"""
    return bool(_SECURITY.search(subject or ""))


def _baseline(labels, bulk=False, direct=False, machine=False,
              security=False):
    """A first-pass importance from signals that cost nothing to read.

    Deliberately dull and explainable. It exists so the inbox is useful before
    any model has run, and so there is always a reason to show -- a score with
    no reason can only be trusted blindly or ignored.

    Three signals, none of them Gmail's own labels, do most of the work:

    List-Unsubscribe means the mail is bulk by its own admission. A name in To
    means it was aimed at you rather than at a list. And a sender local part
    like no-reply or shipment-tracking means nobody typed it -- which matters,
    because an order confirmation is addressed to you personally and is still
    not someone asking you for something.

    Without the third, every Amazon dispatch note outranks a note from a
    friend, which is precisely the ranking an inbox is supposed to fix.
    """
    labels = set(labels or [])
    promo = "CATEGORY_PROMOTIONS" in labels or "CATEGORY_SOCIAL" in labels
    unread = "UNREAD" in labels

    if "STARRED" in labels:
        return 5, "you starred it"

    # Above the machine rule below, which would otherwise cap these at 3 --
    # and they are machine-sent by definition. An account being signed into
    # is the one automated notice worth interrupting for.
    #
    # Bulk and spam are excluded rather than scored, because they are the two
    # ways this band could be claimed by something that merely says the
    # words. A real alert is transactional and carries no unsubscribe link,
    # so requiring that costs nothing genuine and shuts the door on
    # marketing -- and on the phishing that imitates it, which is worth
    # noticing is the same shape.
    if security and not bulk and "SPAM" not in labels:
        return 5, "a security alert about one of your accounts"
    if "IMPORTANT" in labels and unread and not machine:
        return 4, "Gmail marked this important and it is unread"
    if "IMPORTANT" in labels:
        return 3, "Gmail marked this important"

    if bulk and promo:
        return 1, "a promotional mailing you can unsubscribe from"
    if bulk:
        return 2, "a mailing list or newsletter"
    if promo:
        return 2, "promotional or social mail"

    # Addressed to you, but by a machine: worth seeing, not worth interrupting
    # for. The top band is kept for mail a person actually wrote.
    if machine:
        return 3 if direct else 2, ("an automated notice addressed to you"
                                    if direct else "an automated notice")

    if direct and unread:
        return 4, "addressed to you directly, and unread"
    if direct:
        return 3, "addressed to you directly"
    if unread:
        return 3, "unread, and not a mailing"
    return 2, "read, and not a mailing"


def fetch_read_uids(conn, account_id, days=MAIL_WINDOW_DAYS):
    """Which recent messages Gmail considers read.

    fetch_messages asks for the newest forty and fetches metadata for each,
    which is a request per message and therefore has to stay small. At thirty
    messages a day that is about a day of history -- so reading anything
    older on a phone was never noticed here, and it sat unread for ever. On
    this mailbox that was a hundred and sixty-three of two hundred and three.

    This is the cheap half of the problem. Asking for ids alone costs one
    listing rather than one request per message, so it can cover the whole
    window: five hundred ids a page, and the read flag is the answer.
    """
    out, page = set(), None
    while True:
        params = {"maxResults": READ_SWEEP_PAGE,
                  "q": "-in:chats is:read newer_than:%dd" % int(days)}
        if page:
            params["pageToken"] = page
        data = _get_retrying(conn, account_id, GMAIL_LIST, params)
        for stub in data.get("messages", []) or []:
            if stub.get("id"):
                out.add(stub["id"])
        page = data.get("nextPageToken")
        if not page:
            return out


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
        score, reason = _baseline(
            labels, bulk=bulk, direct=direct, machine=_is_machine(addr),
            security=_is_security(_header(headers, "Subject")))
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
