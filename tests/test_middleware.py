import pytest


@pytest.mark.anyio
async def test_request_id_in_response(client):
    resp = await client.get("/health")
    assert "x-request-id" in resp.headers
    rid = resp.headers["x-request-id"]
    assert len(rid) == 36  # UUID format


@pytest.mark.anyio
async def test_custom_request_id_forwarded(client):
    resp = await client.get("/health", headers={"x-request-id": "my-custom-id"})
    assert resp.headers["x-request-id"] == "my-custom-id"


@pytest.mark.anyio
async def test_error_responses_have_request_id(client):
    resp = await client.get("/nonexistent")
    assert "x-request-id" in resp.headers


@pytest.mark.anyio
async def test_unique_request_ids(client):
    r1 = await client.get("/health")
    r2 = await client.get("/health")
    assert r1.headers["x-request-id"] != r2.headers["x-request-id"]
