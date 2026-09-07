"""Fetching remote images on the browser's behalf.

Two problems solved at once.

The content security policy allows images only from this origin, which is what
stops injected markup pulling anything it likes. Remote images in email are
therefore blocked outright, and loosening the policy to allow them would
weaken it for the whole application to benefit one screen.

And a remote image in an email is a tracking pixel whether or not it looks
like one. Loading it from your browser tells the sender your device's address,
when you opened the message, and roughly where you are. Fetching it from here
instead tells them only that Eva asked, which is what every serious mail
client does.

The risk this introduces is a server that fetches URLs on request, so:

* Only URLs this server itself signed are fetched. Without that it would be an
  open proxy on the home network, usable by anyone who can reach the port.
* Addresses inside the network are refused, at every redirect hop as well as
  the first request. That is the difference between a proxy and a way to read
  the router's admin page.
* Only images come back, capped and with a timeout.
"""
import hmac
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from hashlib import sha256

MAX_BYTES = 8 * 1024 * 1024
TIMEOUT = 12
MAX_REDIRECTS = 3
ALLOWED_TYPES = ("image/",)


class Refused(Exception):
    pass


# ── signing ───────────────────────────────────────────────────────────────
def _key(conn):
    """A signing key that survives restarts, kept beside the schema version.

    Per-process would mean every link in every open message broke the moment
    the service restarted.
    """
    row = conn.execute("SELECT value FROM meta WHERE key='image_key'").fetchone()
    if row and row["value"]:
        return row["value"].encode()
    import secrets
    made = secrets.token_urlsafe(32)
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('image_key', ?)",
                 (made,))
    conn.commit()
    return made.encode()


def sign(conn, url):
    return hmac.new(_key(conn), url.encode(), sha256).hexdigest()[:32]


def signed_path(conn, url):
    """The path the page should request instead of the remote URL."""
    return "/api/image?u=%s&s=%s" % (
        urllib.parse.quote(url, safe=""), sign(conn, url))


def verify(conn, url, signature):
    return hmac.compare_digest(sign(conn, url), str(signature or ""))


# ── where we refuse to go ─────────────────────────────────────────────────
def _public(host):
    """Every address a hostname resolves to must be outside this network.

    Checking one address would miss a name that resolves to both a public and
    a private one, which is the whole trick behind DNS rebinding.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise Refused("could not resolve that host") from None
    if not infos:
        raise Refused("could not resolve that host")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise Refused("that address is inside the network")
    return True


def check(url):
    parts = urllib.parse.urlparse(url)
    if parts.scheme not in ("http", "https"):
        raise Refused("only http and https")
    if not parts.hostname:
        raise Refused("no host")
    _public(parts.hostname)
    return True


class _Guard(urllib.request.HTTPRedirectHandler):
    """A redirect is a second request, so it gets the same scrutiny.

    Without this, a public URL that redirects to 127.0.0.1 walks straight past
    the check on the original.
    """
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_Guard())


def fetch(url):
    """Return (content_type, bytes). Raises Refused for anything else."""
    check(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; personal-dashboard)",
        "Accept": "image/*",
    })
    try:
        with _opener.open(req, timeout=TIMEOUT) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not ctype.startswith(ALLOWED_TYPES):
                raise Refused("that is not an image")
            declared = r.headers.get("Content-Length")
            if declared and int(declared) > MAX_BYTES:
                raise Refused("that image is too large")
            # Read one byte past the cap so a lying Content-Length is caught.
            data = r.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise Refused("that image is too large")
            return ctype, data
    except urllib.error.HTTPError as e:
        raise Refused("the sender's server said %s" % e.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise Refused("could not fetch it: %s" % getattr(e, "reason", e)) from None
