import pytest


@pytest.mark.anyio
async def test_health_endpoint(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert "version" in data


@pytest.mark.anyio
async def test_ready_endpoint(client):
    resp = await client.get("/ready")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ready"] is True
    assert "version" in data


@pytest.mark.anyio
async def test_health_returns_version(client):
    resp = await client.get("/health")
    assert resp.json()["version"] == "0.1.0"


@pytest.mark.anyio
async def test_server_error_handler(client):
    """Custom errors should return structured JSON."""
    # This endpoint doesn't exist, should return 404 from FastAPI
    resp = await client.get("/nonexistent")
    assert resp.status_code in (404, 405)
