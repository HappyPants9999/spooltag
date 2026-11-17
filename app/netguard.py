"""Outgoing-request guard (SSRF protection) for the Spoolman, Moonraker and Bambuddy URLs.

spooltag only ever needs to reach services on your own network, so every outgoing request is
checked *after* DNS resolution, and the connection is pinned to the address that was checked
(a name can't resolve to a safe address for the check and a different one for the connect).

Allowed:  private LAN ranges (10/8, 172.16/12, 192.168/16), CGNAT/Tailscale (100.64/10),
          loopback, IPv6 unique-local (fc00::/7) and loopback. Docker service names and
          .lan/.local names resolve into these, so they keep working.
Optional: public internet addresses (Settings → General, off by default).
Never:    link-local (169.254/16, fe80::/10, which includes cloud metadata services),
          known metadata addresses, unspecified, multicast, broadcast and reserved ranges.
"""
import asyncio
import ipaddress
import socket
from typing import Awaitable, Callable, Optional
from urllib.parse import urlsplit

import httpx

IP = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]

_LOCAL = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "127.0.0.0/8",
    "::1/128", "fc00::/7")]
_NEVER = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "169.254.0.0/16", "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32",
    "100.100.100.200/32",                   # Alibaba Cloud metadata (inside 100.64/10)
    "::/128", "fe80::/10", "ff00::/8", "fd00:ec2::254/128")]   # AWS IPv6 metadata (inside fc00::/7)


def classify(address: str) -> str:
    """'local', 'public' or 'blocked' for an IP address string."""
    try:
        ip: IP = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return "blocked"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip.sixtofour or ip.teredo:
            return "blocked"
    if any(ip in net for net in _NEVER):
        return "blocked"
    if any(ip in net for net in _LOCAL):
        return "local"
    return "public" if ip.is_global else "blocked"


def address_error(address: str, allow_public: bool) -> Optional[str]:
    kind = classify(address)
    if kind == "local" or (kind == "public" and allow_public):
        return None
    if kind == "public":
        return f"{address} is a public internet address (allow it in Settings → General if you mean it)"
    return f"{address} is a link-local, metadata or reserved address"


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
        return True
    except ValueError:
        return False


async def system_resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr not in seen:
            seen.append(addr)
    return seen


def url_shape_error(url: str) -> Optional[str]:
    """Problems visible without DNS: scheme, credentials in the URL, missing host."""
    try:
        parts = urlsplit(url)
        parts.port                                  # noqa: B018 - raises on a bad port
    except ValueError:
        return "isn't a valid URL"
    if parts.scheme not in ("http", "https"):
        return "must start with http:// or https://"
    if not parts.hostname:
        return "has no host name"
    if parts.username or parts.password:
        return "must not contain a user name or password (use the API key field)"
    return None


async def check_url(url: str, allow_public: bool, resolver: Resolver = system_resolve) -> Optional[str]:
    """Save-time check. A name that doesn't resolve right now (a stopped Docker service) is
    accepted; every request is checked again when it is made."""
    if err := url_shape_error(url):
        return err
    parts = urlsplit(url)
    host = parts.hostname or ""
    if is_ip_literal(host):
        return address_error(host, allow_public)
    try:
        addresses = await resolver(host, parts.port or (443 if parts.scheme == "https" else 80))
    except (OSError, UnicodeError):
        return None
    for address in addresses:
        if err := address_error(address, allow_public):
            return f"{host} resolves to {err}"
    return None


class GuardedTransport(httpx.AsyncBaseTransport):
    """Resolve, check every address, then connect to the checked address. The Host header and
    TLS SNI keep the original name."""

    def __init__(self, inner: httpx.AsyncBaseTransport, allow_public: Callable[[], bool],
                 resolver: Resolver = system_resolve):
        self.inner = inner
        self.allow_public = allow_public
        self.resolver = resolver

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        host = url.host
        if url.scheme not in ("http", "https"):
            raise httpx.ConnectError(f"blocked: scheme {url.scheme}", request=request)
        if is_ip_literal(host):
            addresses = [host]
        else:
            try:
                addresses = await self.resolver(host, url.port or (443 if url.scheme == "https" else 80))
            except (OSError, UnicodeError) as e:
                raise httpx.ConnectError(f"can't resolve {host} ({e})", request=request) from e
            if not addresses:
                raise httpx.ConnectError(f"can't resolve {host}", request=request)
        for address in addresses:
            if err := address_error(address, self.allow_public()):
                raise httpx.ConnectError(f"blocked: {host} → {err}", request=request)
        if not is_ip_literal(host):
            request.url = url.copy_with(host=addresses[0])
            if url.scheme == "https":
                request.extensions = {**request.extensions, "sni_hostname": host}
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()
