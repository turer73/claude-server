"""SSRF guard testleri — _assert_safe_url (Codex P1 regresyon korumasi).

Kritik: DNS adi private-IP'ye resolve ettiginde guard ENGELLEMELI.
Onceki fix (a062c49) fonksiyonu async yapmis ama DNS resolution eklememis;
literal-olmayan host'ta sessizce return ediyordu (NO-OP). Bu testler o
regresyonu yakalar.
"""

import socket

import pytest

from app.core.network_proxy import _assert_safe_url


def _gai(ip: str):
    """socket.getaddrinfo doner-format taklidi: (family, type, proto, canon, sockaddr)."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]


async def test_literal_loopback_blocked():
    with pytest.raises(ValueError, match="engellenen IP"):
        await _assert_safe_url("http://127.0.0.1/")


async def test_literal_rfc1918_blocked():
    with pytest.raises(ValueError, match="engellenen IP"):
        await _assert_safe_url("http://192.168.1.113/admin")


async def test_literal_link_local_metadata_blocked():
    with pytest.raises(ValueError, match="engellenen IP"):
        await _assert_safe_url("http://169.254.169.254/latest/meta-data/")


async def test_blocked_host_localhost():
    with pytest.raises(ValueError, match="engellenen host"):
        await _assert_safe_url("http://localhost:8420/")


async def test_empty_host_rejected():
    with pytest.raises(ValueError, match="host yok"):
        await _assert_safe_url("not-a-url")


async def test_literal_public_ip_allowed():
    await _assert_safe_url("http://1.1.1.1/")  # raise etmemeli


async def test_dns_name_resolving_private_blocked(monkeypatch):
    """KRITIK (P1): private-IP'ye resolve eden domain engellenmeli."""
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _gai("169.254.169.254"))
    with pytest.raises(ValueError, match="engellenen IP"):
        await _assert_safe_url("http://metadata.example.com/")


async def test_dns_name_resolving_loopback_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _gai("127.0.0.1"))
    with pytest.raises(ValueError, match="engellenen IP"):
        await _assert_safe_url("http://evil.example.com/")


async def test_dns_name_resolving_public_allowed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _gai("93.184.216.34"))
    await _assert_safe_url("http://example.com/")  # raise etmemeli


async def test_dns_unresolvable_rejected(monkeypatch):
    def _boom(*a, **k):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    with pytest.raises(ValueError, match="cozumlenemedi"):
        await _assert_safe_url("http://nonexistent.invalid/")
