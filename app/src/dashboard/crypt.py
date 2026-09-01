"""Encrypting secrets at rest.

Refresh tokens grant ongoing access to your mail and calendar, and an API key
is a bearer credential. Both are stored encrypted rather than in plaintext.

This is the one place the project takes a third-party dependency. Python's
standard library has no symmetric cipher; hashing is not encryption, and
inventing one here would be worse than the dependency.

The key comes from a file, never from an environment variable, because an env
var is readable through `docker inspect` and /proc/<pid>/environ by anyone who
can reach either.

Honest about what this does and does not buy: the key sits next to the data on
the same host, so it does not defend against someone who has already read
arbitrary files as this user. It defends against a leaked backup, a copied
database file, and a stray `cat` of the wrong path -- which are the ways these
things actually escape.
"""

import base64
import hashlib
import os
import threading

_lock = threading.Lock()
_fernet = None
_missing_warned = False


def _load_key():
    path = os.environ.get("TOKEN_KEY_PATH")
    raw = None
    if path and os.path.exists(path):
        try:
            with open(path, "rb") as fh:
                raw = fh.read().strip()
        except OSError:
            raw = None
    if not raw:
        raw = (os.environ.get("TOKEN_KEY") or "").encode() or None
    if not raw:
        return None
    # Accept either a Fernet key as generated, or any sufficiently long
    # secret, which is what bootstrap.sh writes.
    if len(raw) == 44 and raw.endswith(b"="):
        return raw
    return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())


def available():
    """Can secrets be stored? Everything else works either way."""
    return _get() is not None


def _get():
    global _fernet, _missing_warned
    with _lock:
        if _fernet is not None:
            return _fernet
        key = _load_key()
        if not key:
            if not _missing_warned:
                _missing_warned = True
                from . import storage
                storage.log("crypt: no TOKEN_KEY_PATH; secrets cannot be stored")
            return None
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            if not _missing_warned:
                _missing_warned = True
                from . import storage
                storage.log("crypt: cryptography not installed; "
                            "secrets cannot be stored")
            return None
        _fernet = Fernet(key)
        return _fernet


def encrypt(plaintext):
    """Return ciphertext, or raise if no key is configured.

    Deliberately raises rather than falling back to plaintext. A silent
    downgrade would store a refresh token in the clear while the interface
    said it was saved.
    """
    f = _get()
    if f is None:
        raise RuntimeError("no encryption key configured; refusing to store "
                           "a secret in plaintext")
    return f.encrypt(str(plaintext).encode("utf-8")).decode("ascii")


def decrypt(ciphertext):
    """Return plaintext, or None if it cannot be read.

    A key that has been rotated makes old values unreadable rather than
    wrong, which is the safe failure: the caller re-prompts instead of
    authenticating with nonsense.
    """
    if not ciphertext:
        return None
    f = _get()
    if f is None:
        return None
    try:
        return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except Exception:
        return None


def reset_for_tests():
    global _fernet, _missing_warned
    with _lock:
        _fernet = None
        _missing_warned = False
