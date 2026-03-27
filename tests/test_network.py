import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.core.network_proxy import NetworkProxy


@pytest.fixture
def proxy():
    return NetworkProxy()


def test_get_interfaces(proxy):
    interfaces = proxy.get_interfaces()
    assert isinstance(interfaces, list)
    assert len(interfaces) > 0
    iface = interfaces[0]
    assert "name" in iface
    assert "addresses" in iface


def test_get_connections(proxy):
    conns = proxy.get_connections()
    assert isinstance(conns, list)


@pytest.mark.anyio
async def test_dns_lookup(proxy):
    result = await proxy.dns_lookup("localhost")
    assert isinstance(result, list)
    assert len(result) > 0


@pytest.mark.anyio
async def test_dns_lookup_invalid(proxy):
    result = await proxy.dns_lookup("thisdomain.definitely.does.not.exist.invalid")
    assert isinstance(result, list)
    # May be empty on DNS failure


@pytest.mark.anyio
async def test_http_request_mock(proxy):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "text/plain"}
    mock_response.text = "OK"
    mock_response.elapsed = MagicMock()
    mock_response.elapsed.total_seconds = MagicMock(return_value=0.1)

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock, return_value=mock_response):
        result = await proxy.http_request("GET", "https://example.com")
        assert result["status_code"] == 200
        assert result["body"] == "OK"
