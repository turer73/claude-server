from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.webops_proxy import WebOpsProxy


@pytest.fixture
def proxy():
    return WebOpsProxy(
        vercel_token="test-vercel",
        cloudflare_token="test-cf",
        github_token="test-gh",
        supabase_url="https://test.supabase.co",
        supabase_token="test-sb",
        coolify_url="https://coolify.test",
        coolify_token="test-coolify",
    )


def test_proxy_init(proxy):
    assert proxy._tokens["vercel"] == "test-vercel"
    assert proxy._tokens["github"] == "test-gh"


def test_get_headers_vercel(proxy):
    headers = proxy.get_headers("vercel")
    assert headers["Authorization"] == "Bearer test-vercel"


def test_get_headers_github(proxy):
    headers = proxy.get_headers("github")
    assert headers["Authorization"] == "Bearer test-gh"
    assert headers["Accept"] == "application/vnd.github+json"


def test_get_headers_cloudflare(proxy):
    headers = proxy.get_headers("cloudflare")
    assert headers["Authorization"] == "Bearer test-cf"


def test_get_headers_unknown(proxy):
    headers = proxy.get_headers("unknown")
    assert headers == {}


def test_get_base_url(proxy):
    assert "vercel.com" in proxy.get_base_url("vercel")
    assert "cloudflare.com" in proxy.get_base_url("cloudflare")
    assert "github.com" in proxy.get_base_url("github")
    assert proxy.get_base_url("coolify") == "https://coolify.test"


def test_available_services(proxy):
    services = proxy.available_services()
    assert "vercel" in services
    assert "cloudflare" in services
    assert "github" in services
    assert "supabase" in services
    assert "coolify" in services


@pytest.mark.anyio
async def test_proxy_request_mock(proxy):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}
    mock_resp.text = '{"projects": []}'

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_resp):
        result = await proxy.request("vercel", "GET", "/v9/projects")
        assert result["status_code"] == 200
        assert "projects" in result["body"]
