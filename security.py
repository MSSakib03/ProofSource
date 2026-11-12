"""
Security helpers for ProofSource.

- CSRF: a per-session token that every state-changing request must send in the
  ``X-CSRF-Token`` header (the SPA reads it from ``window.PS_INITIAL`` / ``/api/session``).
- Security headers: Content-Security-Policy with a per-request nonce, frame / sniffing /
  referrer protections and ``no-store`` for private JSON.
- Encryption at rest for OAuth tokens (Fernet, key derived from the Flask secret key), so a
  copied database file or snapshot does not hand out working Wikimedia tokens.
- Small in-process rate limiter for expensive endpoints (wiki API heavy work).
- URL validation for user-supplied links.
"""
import base64
import hashlib
import hmac
import importlib.util
import logging
import secrets
import threading
import time
from urllib.parse import urlsplit

from flask import g, request, session

CSRF_SESSION_KEY = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Hosts the page may load images from (page scans and images inside rendered wikitext).
IMG_HOSTS = "https://upload.wikimedia.org https://wikimedia.org https://*.wikisource.org https://*.wikimedia.org"


# ---------------------------------------------------------------- CSRF

def csrf_token() -> str:
    tok = session.get(CSRF_SESSION_KEY)
    if not tok:
        tok = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = tok
    return tok


def rotate_csrf_token() -> str:
    session.pop(CSRF_SESSION_KEY, None)
    return csrf_token()


def csrf_failure_reason(allowed_hosts=()):
    """Returns None when the request is allowed, else a short reason code.
    `allowed_hosts`: extra public host names (e.g. from OAUTH2_REDIRECT_URI) in case a proxy
    rewrites the Host header."""
    if request.method in SAFE_METHODS:
        return None
    # Browsers tell us when a request comes from another site; refuse those outright.
    if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        return "cross_site"
    origin = request.headers.get("Origin")
    if origin and origin != "null":
        host = urlsplit(origin).netloc.lower()
        if host and host != request.host.lower() and host not in allowed_hosts:
            return "bad_origin"
    sent = request.headers.get(CSRF_HEADER, "")
    expected = session.get(CSRF_SESSION_KEY, "")
    if not sent or not expected or not hmac.compare_digest(sent, expected):
        return "bad_token"
    return None


# ---------------------------------------------------------------- headers

def csp_nonce() -> str:
    n = getattr(g, "_csp_nonce", None)
    if not n:
        n = secrets.token_urlsafe(18)
        g._csp_nonce = n
    return n


def apply_security_headers(resp, https: bool = True):
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
    if https:
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    if resp.mimetype == "text/html":
        nonce = getattr(g, "_csp_nonce", None)
        script_src = "'self' 'unsafe-eval'" + (f" 'nonce-{nonce}'" if nonce else "")
        # 'unsafe-eval' is required by Vue's in-browser template compiler (no build step).
        h.setdefault("Content-Security-Policy", "; ".join([
            "default-src 'self'",
            f"script-src {script_src}",
            "style-src 'self' 'unsafe-inline'",
            f"img-src 'self' data: {IMG_HOSTS}",
            "font-src 'self'",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ]))
    path = request.path or ""
    if path.startswith("/api/") or path in ("/login", "/logout", "/auth/callback", "/oauth-callback"):
        h["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------- token encryption at rest

_ENC_PREFIX = "enc1:"
_log = logging.getLogger("proofsource")
_warned_no_crypto = [False]
CRYPTO_MISSING_MSG = ("The 'cryptography' package is not installed, so OAuth tokens are stored WITHOUT "
                      "encryption. Run `pip install -r requirements.txt` in the tool's virtualenv and restart.")


def crypto_available() -> bool:
    try:
        return importlib.util.find_spec("cryptography.fernet") is not None
    except (ImportError, ValueError):
        return False


def _warn_no_crypto():
    if not _warned_no_crypto[0]:
        _warned_no_crypto[0] = True
        _log.error(CRYPTO_MISSING_MSG)


def _fernet(secret_key: str):
    """Fernet instance, or None when `cryptography` is missing (an update deployed without
    reinstalling requirements must never break login)."""
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        _warn_no_crypto()
        return None
    raw = hashlib.sha256(("proofsource-token-encryption:" + secret_key).encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt_secret(value, secret_key: str):
    if value is None or value == "":
        return value
    f = _fernet(secret_key)
    if f is None:
        return value   # degraded mode: same as before encryption existed; logged loudly
    return _ENC_PREFIX + f.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value, secret_key: str):
    """Decrypts a stored token. Legacy plaintext rows are returned unchanged.
    Returns None when the value cannot be decrypted (secret key rotated, or `cryptography`
    missing): the user is then simply asked to log in again."""
    if not value or not value.startswith(_ENC_PREFIX):
        return value
    f = _fernet(secret_key)
    if f is None:
        return None
    from cryptography.fernet import InvalidToken
    try:
        return f.decrypt(value[len(_ENC_PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def sign(data: str, secret_key: str) -> str:
    return hmac.new(("proofsource-backup:" + secret_key).encode("utf-8"), data.encode("utf-8"),
                    hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- rate limiting

class RateLimiter:
    """Sliding-window limiter, per process. Good enough to stop one user (or a script)
    from hammering the wiki-API-heavy endpoints; uWSGI workers each keep their own window."""

    def __init__(self):
        self._hits = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, per_seconds: float) -> bool:
        """Records a hit; returns False when the key is over its limit."""
        now = time.monotonic()
        with self._lock:
            q = [t for t in self._hits.get(key, ()) if now - t < per_seconds]
            if len(q) >= limit:
                self._hits[key] = q
                return False
            q.append(now)
            self._hits[key] = q
            if len(self._hits) > 5000:   # keep memory bounded
                for k in list(self._hits)[:1000]:
                    self._hits.pop(k, None)
            return True

    def reset(self):
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


# ---------------------------------------------------------------- URLs

def safe_http_url(raw, max_len: int = 500) -> str:
    """Returns the URL if it is a plain http(s) link, else ''. Blocks javascript:, data: etc."""
    url = (raw or "").strip()
    if not url or len(url) > max_len or any(c in url for c in "\r\n\t\"'<> "):
        return ""
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return ""
    return url
