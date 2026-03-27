import pytest


@pytest.mark.anyio
async def test_token_endpoint(client):
    resp = await client.post("/api/v1/auth/token", json={"api_key": "any-key"})
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert "expires_in" in data


@pytest.mark.anyio
async def test_me_endpoint_with_token(client):
    # Get token first
    resp = await client.post("/api/v1/auth/token", json={"api_key": "any-key"})
    token = resp.json()["access_token"]

    # Use token
    resp2 = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert resp2.status_code == 200
    data = resp2.json()
    assert "name" in data
    assert "permissions" in data


@pytest.mark.anyio
async def test_me_without_auth_header(client):
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 422  # missing required header


@pytest.mark.anyio
async def test_me_with_invalid_token(client):
    resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer invalid-token"}
    )
    assert resp.status_code == 401
