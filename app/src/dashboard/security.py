"""Response headers and CSRF.

Two independent controls sit between untrusted text and the browser:

  The content security policy forbids inline script entirely, so injected
  markup has no way to execute even if it reaches the DOM.

  The front end never assigns markup -- textContent only, enforced by CI.

Either alone would probably hold. Both together means a mistake in one is not
a vulnerability, which matters because this renders email subjects, sender
names, transaction payees and model-written text, none of which the user
controls.
"""

import hmac
import secrets

# No 'unsafe-inline' anywhere. That is a real constraint on the UI -- no inline
# <script>, no style="..." -- and it is the point: a policy with unsafe-inline
# in script-src stops almost nothing.
CSP = "; ".join([
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
])

HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # Nothing here needs a camera, a microphone or a location.
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Cache-Control": "no-store",
}

# Only sent over TLS; harmless and ignored on plain HTTP. Separate so a
# localhost development run does not pin HSTS on the developer's browser.
HSTS = {"Strict-Transport-Security": "max-age=31536000"}


def response_headers(secure=False):
    h = dict(HEADERS)
    if secure:
        h.update(HSTS)
    return h


def new_token(nbytes=32):
    return secrets.token_urlsafe(nbytes)


def csrf_ok(expected, submitted):
    """Constant-time comparison.

    A plain == leaks how many leading characters matched through timing.
    Cheap to avoid, so there is no reason to leave it.
    """
    if not expected or not submitted:
        return False
    return hmac.compare_digest(str(expected), str(submitted))


def cookie(name, value, secure=True, max_age=None, path="/"):
    """Build a Set-Cookie value.

    HttpOnly: script cannot read the session id, so an XSS bug does not
    immediately become account theft.
    SameSite=Strict: a request originating from another site never carries
    this cookie, which stops CSRF before the token check is even reached.
    """
    parts = ["%s=%s" % (name, value), "Path=%s" % path, "HttpOnly",
             "SameSite=Strict"]
    if secure:
        parts.append("Secure")
    if max_age is not None:
        parts.append("Max-Age=%d" % max_age)
    return "; ".join(parts)


def expire_cookie(name, secure=True, path="/"):
    return cookie(name, "", secure=secure, max_age=0, path=path)
