"""Network proxy — HTTP requests, interfaces, DNS, ping."""

from __future__ import annotations

import asyncio
import socket
import time

import httpx
import psutil


class NetworkProxy:
    """Network operations proxy."""

    def get_interfaces(self) -> list[dict]:
        result = []
        addrs = psutil.net_if_addrs()
        for name, addr_list in addrs.items():
            addresses = []
            for addr in addr_list:
                addresses.append({
                    "family": str(addr.family.name) if hasattr(addr.family, "name") else str(addr.family),
                    "address": addr.address,
                    "netmask": addr.netmask,
                })
            result.append({"name": name, "addresses": addresses})
        return result

    def get_connections(self) -> list[dict]:
        conns = []
        for c in psutil.net_connections(kind="inet"):
            try:
                conns.append({
                    "fd": c.fd,
                    "family": str(c.family),
                    "type": str(c.type),
                    "laddr": f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else "",
                    "raddr": f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else "",
                    "status": c.status,
                    "pid": c.pid,
                })
            except (AttributeError, TypeError):
                continue
        return conns

    async def dns_lookup(self, hostname: str) -> list[dict]:
        try:
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(
                None, lambda: socket.getaddrinfo(hostname, None)
            )
            seen = set()
            entries = []
            for family, socktype, proto, canonname, sockaddr in results:
                ip = sockaddr[0]
                if ip not in seen:
                    seen.add(ip)
                    entries.append({
                        "ip": ip,
                        "family": "IPv4" if family == socket.AF_INET else "IPv6",
                    })
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
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
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
