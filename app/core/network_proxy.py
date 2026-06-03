"""Network proxy — HTTP requests, interfaces, DNS, ping."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time

import httpx
import psutil

# GUVENLIK: SSRF guard - bu ag araliklaarina HTTP istegi engellenir.
_SSRF_BLOCKED: tuple = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)
_SSRF_BLOCKED_HOSTS = frozenset({"localhost", "metadata.google.internal", "metadata.internal"})


async def _assert_safe_url(url: str) -> None:
    """SSRF guard: private/loopback/link-local hedefleri reddeder. ValueError firlatir."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError(f"SSRF-guard: URL gecersiz veya host yok: {url!r}")
    if host in _SSRF_BLOCKED_HOSTS:
        raise ValueError(f"SSRF-guard: engellenen host: {host!r}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    for net in _SSRF_BLOCKED:
        if ip in net:
            raise ValueError(f"SSRF-guard: engellenen IP ({ip} in {net})")


class NetworkProxy:
    """Network operations proxy."""

    def get_interfaces(self) -> list[dict]:
        result = []
        addrs = psutil.net_if_addrs()
        for name, addr_list in addrs.items():
            addresses = []
            for addr in addr_list:
                addresses.append(
                    {
                        "family": str(addr.family.name) if hasattr(addr.family, "name") else str(addr.family),
                        "address": addr.address,
                        "netmask": addr.netmask,
                    }
                )
            result.append({"name": name, "addresses": addresses})
        return result

    def get_connections(self) -> list[dict]:
        conns = []
        for c in psutil.net_connections(kind="inet"):
            try:
                conns.append(
                    {
                        "fd": c.fd,
                        "family": str(c.family),
                        "type": str(c.type),
                        "laddr": f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else "",
                        "raddr": f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else "",
                        "status": c.status,
                        "pid": c.pid,
                    }
                )
            except (AttributeError, TypeError):
                continue
        return conns

    async def dns_lookup(self, hostname: str) -> list[dict]:
        try:
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(None, lambda: socket.getaddrinfo(hostname, None))
            seen = set()
            entries = []
            for family, socktype, proto, canonname, sockaddr in results:
                ip = sockaddr[0]
                if ip not in seen:
                    seen.add(ip)
                    entries.append(
                        {
                            "ip": ip,
                            "family": "IPv4" if family == socket.AF_INET else "IPv6",
                        }
                    )
            return entries
        except socket.gaierror:
            return []

    async def http_request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: str | None = None,
        timeout: int = 30,
    ) -> dict:
        start = time.monotonic()
        await _assert_safe_url(url)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.request(
                method=method,
                url=url,
                headers=headers,
                content=body,
            )
        elapsed = (time.monotonic() - start) * 1000
        return {
            "status_code": resp.status_code,
            "headers": dict(resp.headers),
            "body": resp.text[:100000],  # cap response
            "elapsed_ms": round(elapsed, 1),
        }
