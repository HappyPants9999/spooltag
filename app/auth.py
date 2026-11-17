"""Optional admin password for Settings and admin actions. Tag scans never need it.

No password set = no login (the 0.x behaviour). The password is stored as an scrypt hash in
the settings table; a login creates a random session token whose SHA-256 is stored, sent as an
HttpOnly, SameSite=Strict cookie. Forgotten password: `docker exec spooltag python -m app.admin
reset-password`.
"""
import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from typing import Optional

from fastapi import Request

from .store import Store

HASH_KEY = "admin_password_hash"
SESSION_COOKIE = "spooltag_session"
SESSION_DAYS = 30
MIN_LENGTH = 8
_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r),
                                p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def password_set(store: Store) -> bool:
    return bool(store.get_setting(HASH_KEY))


def new_session(store: Store) -> str:
    token = secrets.token_urlsafe(32)
    store.add_session(token_hash(token), time.time() + SESSION_DAYS * 86400)
    return token


def is_admin(request: Request, store: Store) -> bool:
    if not password_set(store):
        return True
    token = request.cookies.get(SESSION_COOKIE, "")
    return bool(token) and len(token) < 200 and store.session_valid(token_hash(token))


def safe_next(target: Optional[str], default: str = "/settings") -> str:
    """Only a path on this site: no scheme, host, '//' or backslash tricks (open redirect)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return default
    if any(ord(c) < 0x20 for c in target):
        return default
    return target


class LoginLimiter:
    """At most `limit` failed password attempts per client address per `window` seconds."""

    def __init__(self, limit: int = 5, window: float = 900):
        self.limit, self.window = limit, window
        self.failures: dict[str, deque] = defaultdict(deque)

    def _trim(self, key: str) -> deque:
        q = self.failures[key]
        while q and time.time() - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        return len(self._trim(key)) >= self.limit

    def fail(self, key: str) -> None:
        self._trim(key).append(time.time())

    def reset(self, key: str) -> None:
        self.failures.pop(key, None)
