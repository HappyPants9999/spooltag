"""Browser-facing protections: Host allowlist (DNS rebinding), cross-site request checks (CSRF),
security headers, and the per-device scan cookie.

Why fetch metadata rather than tokens for scans: an NFC tag holds a fixed URL, so it can't carry
a token. A tag tap (Android Chrome via the NFC intent, iOS Safari from the tag notification), a
QR scan, a bookmark or a typed URL is a browser-initiated navigation: `Sec-Fetch-Site: none`.
A request a website causes - an <img>, fetch(), a link, a redirect, window.open - is `same-site`
or `cross-site`. Only `none` and `same-origin` may act at once; anything else gets a confirm page
(navigations) or a 403 (everything else). Browsers too old to send fetch metadata (before
Chrome 76 / Safari 16.4) fall back to the Referer and Origin headers.
"""
import ipaddress
import re
import secrets
from typing import Optional
from urllib.parse import urlsplit

from fastapi import Request

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

CSP = ("default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src 'self' data:; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

DEVICE_COOKIE = "spooltag_device"
DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def host_only(value: str) -> str:
    """'Example.LAN:7913' -> 'example.lan'; '[fd00::1]:80' -> 'fd00::1'."""
    value = value.strip().lower()
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    if value.count(":") == 1:
        return value.split(":", 1)[0]
    return value


def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        return True
    except ValueError:
        return False


def host_allowed(host_header: str, allowed: set[str]) -> bool:
    """IP addresses are always allowed: DNS rebinding needs a host *name* (the attacker's), so the
    browser sends that name as Host. Names must be listed (tag base URL, Settings, ALLOWED_HOSTS)."""
    if "*" in allowed:
        return True
    host = host_only(host_header)
    return bool(host) and (is_ip(host) or host in allowed)


def _netloc(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower()
    except ValueError:
        return "\0"


def _own_netloc(request: Request) -> str:
    return request.headers.get("host", "").lower()


def unsafe_request_allowed(request: Request) -> bool:
    """CSRF check for POST and friends: only this site's own pages may send them."""
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site in ("same-origin", "none")
    origin = request.headers.get("origin")
    if origin is not None:
        return origin != "null" and _netloc(origin) == _own_netloc(request)
    referer = request.headers.get("referer")
    if referer:
        return _netloc(referer) == _own_netloc(request)
    return True     # not a browser (curl, scripts): CSRF needs a victim's browser


def scan_is_direct(request: Request) -> bool:
    """A GET scan may act at once only if the browser itself started it (tag, QR, bookmark)."""
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site in ("none", "same-origin")
    referer = request.headers.get("referer")
    if referer:
        return _netloc(referer) == _own_netloc(request)
    return True


def is_top_level_navigation(request: Request) -> bool:
    mode = request.headers.get("sec-fetch-mode")
    if mode is None:
        return True
    return mode == "navigate" and request.headers.get("sec-fetch-dest", "document") == "document"


def device_id(request: Request) -> str:
    """This browser's scan-pairing id (a random cookie). A new one is made if missing."""
    existing = getattr(request.state, "device", None)
    if existing:
        return existing
    value = request.cookies.get(DEVICE_COOKIE, "")
    if not DEVICE_RE.match(value):
        value = secrets.token_urlsafe(18)
        request.state.new_device = True
    request.state.device = value
    return value


def existing_device_id(request: Request) -> Optional[str]:
    value = request.cookies.get(DEVICE_COOKIE, "")
    return value if DEVICE_RE.match(value) else None


def is_https(request: Request) -> bool:
    return request.url.scheme == "https"
