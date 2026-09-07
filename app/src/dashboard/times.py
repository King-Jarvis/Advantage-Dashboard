"""One place that decides what a timestamp means.

The database stores naive UTC throughout. Everything arriving from outside --
Google's RFC 3339 with an offset, a browser's local time, a hand-typed date --
has to be reduced to that before it is stored, or ordering by starts_at is
quietly wrong for anyone who travels or edits during a daylight-saving change.

This lived in google_api and was about to be copied into the event writer.
Two copies of a time conversion is how two parts of one application end up
disagreeing about when something happens.
"""
import datetime as _dt

ISO = "%Y-%m-%dT%H:%M:%S"


def to_utc(value):
    """RFC 3339 with any offset -> naive ISO 8601 in UTC.

    A value with no offset is taken to be UTC already, which is what the
    database holds and what Google is told explicitly on the way out.
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
    return dt.strftime(ISO)


def parse(value):
    """A datetime, or None. Never raises -- callers decide what absence means."""
    text = to_utc(value)
    try:
        return _dt.datetime.strptime(text, ISO)
    except (TypeError, ValueError):
        return None


def plus(value, **kw):
    d = parse(value)
    return (d + _dt.timedelta(**kw)).strftime(ISO) if d else ""


def now():
    return _dt.datetime.now(_dt.UTC).strftime(ISO)
