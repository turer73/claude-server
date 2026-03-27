import pytest

TEST_API_KEY = "test-api-key-for-testing-purposes-1234567890abcdef"


@pytest.mark.anyio
async def test_token_endpoint(client):
    resp = await client.post("/api/v1/auth/token", json={"api_key": TEST_API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert "expires_in" in data


@pytest.mark.anyio
async def test_me_endpoint_with_token(client):
    # Get token first
    resp = await client.post("/api/v1/auth/token", json={"api_key": TEST_API_KEY})
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


@pytest.mark.anyio
async def test_token_with_invalid_key(client):
    """Unknown API key should be rejected."""
    resp = await client.post("/api/v1/auth/token", json={"api_key": "nonexistent-key"})
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_token_with_inactive_key(client):
    """Inactive API key should be rejected."""
    from app.auth.api_key import hash_api_key

    db = client._transport.app.state.db
    inactive_key = "inactive-key-for-testing-purposes-0000000000000000"
    await db.execute(
        "INSERT INTO api_keys (key_hash, name, permissions, active) VALUES (?, ?, ?, ?)",
        (hash_api_key(inactive_key), "inactive-user", "read", 0),
    )

    resp = await client.post("/api/v1/auth/token", json={"api_key": inactive_key})
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_token_updates_last_used(client):
    """Successful auth should update last_used timestamp."""
    from app.auth.api_key import hash_api_key

    resp = await client.post("/api/v1/auth/token", json={"api_key": TEST_API_KEY})
    assert resp.status_code == 200

    db = client._transport.app.state.db
    row = await db.fetch_one(
        "SELECT last_used FROM api_keys WHERE key_hash = ?",
        (hash_api_key(TEST_API_KEY),),
    )
    assert row is not None
    assert row["last_used"] is not None


@pytest.mark.anyio
async def test_token_returns_correct_permissions(client):
    """Token should contain permissions from the database."""
    from app.auth.jwt_handler import decode_token
    from app.core.config import get_settings

    resp = await client.post("/api/v1/auth/token", json={"api_key": TEST_API_KEY})
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    settings = get_settings()
    payload = decode_token(token, settings.jwt_secret)
    assert payload["sub"] == "admin"
    assert payload["permissions"] == "admin"
