"""End-to-end integration tests — full request cycles."""

import pytest

TEST_API_KEY = "test-api-key-for-testing-purposes-1234567890abcdef"


# --- Auth Flow ---


@pytest.mark.anyio
async def test_full_auth_flow(client):
    """API key -> JWT token -> authenticated request -> user info."""
    # Step 1: Get token
    resp = await client.post("/api/v1/auth/token", json={"api_key": TEST_API_KEY})
    assert resp.status_code == 200
    token = resp.json()["access_token"]

    # Step 2: Use token to get /me
    resp2 = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp2.status_code == 200
    assert resp2.json()["permissions"] == "admin"

    # Step 3: Invalid token should fail
    resp3 = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer fake-token"})
    assert resp3.status_code == 401


@pytest.mark.anyio
async def test_auth_token_reuse(client):
    """Same API key should produce valid tokens on multiple requests."""
    resp1 = await client.post("/api/v1/auth/token", json={"api_key": TEST_API_KEY})
    resp2 = await client.post("/api/v1/auth/token", json={"api_key": TEST_API_KEY})
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    # Both tokens must be usable for /me
    for token in (resp1.json()["access_token"], resp2.json()["access_token"]):
        me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert me.json()["permissions"] == "admin"


@pytest.mark.anyio
async def test_auth_invalid_key_rejected(client):
    """Unknown API key must return 401."""
    resp = await client.post("/api/v1/auth/token", json={"api_key": "bogus-key-that-does-not-exist"})
    assert resp.status_code == 401


# --- Kernel ---


@pytest.mark.anyio
async def test_kernel_status_flow(client, auth_headers):
    """Read kernel status -- should work even without module."""
    resp = await client.get("/api/v1/kernel/status", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] in ("running", "unavailable", "unknown")
    assert data["cpu_count"] > 0


@pytest.mark.anyio
async def test_kernel_governor_read(client, auth_headers):
    resp = await client.get("/api/v1/kernel/governor", headers=auth_headers)
    assert resp.status_code == 200
    assert "governor" in resp.json()


# --- System ---


@pytest.mark.anyio
async def test_system_info_flow(client, auth_headers):
    resp = await client.get("/api/v1/system/info", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["cpu_count"] > 0
    assert data["memory_total_mb"] > 0
    assert len(data["hostname"]) > 0


@pytest.mark.anyio
async def test_process_list_flow(client, auth_headers):
    resp = await client.get("/api/v1/system/processes?limit=5", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] <= 5
    assert isinstance(data["processes"], list)


@pytest.mark.anyio
async def test_process_list_default_limit(client, auth_headers):
    """Default limit (20) should return up to 20 processes."""
    resp = await client.get("/api/v1/system/processes", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] <= 20
    assert isinstance(data["processes"], list)
    if data["total"] > 0:
        proc = data["processes"][0]
        assert "pid" in proc
        assert "name" in proc


# --- Files CRUD ---


@pytest.mark.anyio
async def test_file_crud_cycle(client, auth_headers, tmp_path):
    """Create -> Read -> Edit -> Read -> Delete full cycle."""
    from app.core.config import Settings, get_settings

    app = client._transport.app

    def patched_settings():
        return Settings(allowed_paths=[str(tmp_path)])

    app.dependency_overrides[get_settings] = patched_settings
    try:
        test_path = str(tmp_path / "integration_test.txt")

        # Create
        resp = await client.put(
            "/api/v1/files/write",
            headers=auth_headers,
            json={
                "path": test_path,
                "content": "hello world",
                "mode": "write",
            },
        )
        assert resp.status_code == 200

        # Read
        resp = await client.get(f"/api/v1/files/read?path={test_path}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["content"] == "hello world"

        # Edit
        resp = await client.patch(
            "/api/v1/files/edit",
            headers=auth_headers,
            json={
                "path": test_path,
                "old_string": "hello",
                "new_string": "goodbye",
            },
        )
        assert resp.status_code == 200

        # Read again
        resp = await client.get(f"/api/v1/files/read?path={test_path}", headers=auth_headers)
        assert resp.json()["content"] == "goodbye world"

        # Delete
        resp = await client.delete(f"/api/v1/files/delete?path={test_path}", headers=auth_headers)
        assert resp.status_code == 200

        # Verify deleted
        resp = await client.get(f"/api/v1/files/read?path={test_path}", headers=auth_headers)
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.anyio
async def test_file_write_and_append(client, auth_headers, tmp_path):
    """Write then append should concatenate content."""
    from app.core.config import Settings, get_settings

    app = client._transport.app

    def patched_settings():
        return Settings(allowed_paths=[str(tmp_path)])

    app.dependency_overrides[get_settings] = patched_settings
    try:
        test_path = str(tmp_path / "append_test.txt")

        # Write initial
        resp = await client.put(
            "/api/v1/files/write",
            headers=auth_headers,
            json={
                "path": test_path,
                "content": "first\n",
                "mode": "write",
            },
        )
        assert resp.status_code == 200

        # Append
        resp = await client.put(
            "/api/v1/files/write",
            headers=auth_headers,
            json={
                "path": test_path,
                "content": "second\n",
                "mode": "append",
            },
        )
        assert resp.status_code == 200

        # Read and verify
        resp = await client.get(f"/api/v1/files/read?path={test_path}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["content"] == "first\nsecond\n"
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.anyio
async def test_file_read_unauthorized_path(client, auth_headers):
    """Reading a path outside allowed_paths must fail."""
    resp = await client.get("/api/v1/files/read?path=/etc/shadow", headers=auth_headers)
    assert resp.status_code == 403


# --- Monitor ---


@pytest.mark.anyio
async def test_monitor_metrics_flow(client, auth_headers):
    resp = await client.get("/api/v1/monitor/metrics", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "cpu_percent" in data
    assert "memory_percent" in data
    assert isinstance(data["load_avg"], list)


@pytest.mark.anyio
async def test_monitor_metrics_types(client, auth_headers):
    """Metric values should be proper numeric types."""
    resp = await client.get("/api/v1/monitor/metrics", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["cpu_percent"], float)
    assert isinstance(data["memory_percent"], float)
    assert isinstance(data["disk_percent"], float)
    assert isinstance(data["network_sent_mb"], float)
    assert isinstance(data["network_recv_mb"], float)


# --- Logs ---


@pytest.mark.anyio
async def test_logs_sources(client, auth_headers):
    resp = await client.get("/api/v1/logs/sources", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "sources" in data
    assert isinstance(data["sources"], list)


@pytest.mark.anyio
async def test_logs_tail(client, auth_headers):
    """Tail endpoint should return a lines array (possibly empty if no log files exist)."""
    resp = await client.get("/api/v1/logs/tail?n=10", headers=auth_headers)
    assert resp.status_code == 200
    assert "lines" in resp.json()


# --- Health ---


@pytest.mark.anyio
async def test_health_and_ready(client):
    h = await client.get("/health")
    r = await client.get("/ready")
    assert h.status_code == 200
    assert r.status_code == 200
    assert h.json()["status"] == "healthy"
    assert r.json()["ready"] is True


@pytest.mark.anyio
async def test_version_consistency(client):
    resp = await client.get("/health")
    assert resp.json()["version"] == "0.1.0"

    resp2 = await client.get("/ready")
    assert resp2.json()["version"] == "0.1.0"


# --- Agents ---


@pytest.mark.anyio
async def test_agent_crud(client, auth_headers):
    """Create agent -> list -> get -> delete."""
    agent_name = "test-integration-agent"

    # Create
    resp = await client.post(
        "/api/v1/agents/create",
        headers=auth_headers,
        json={
            "name": agent_name,
            "description": "Integration test agent",
            "trigger": "manual",
            "tools": ["shell_exec"],
        },
    )
    assert resp.status_code == 200

    # List
    resp = await client.get("/api/v1/agents/list", headers=auth_headers)
    assert resp.status_code == 200
    agents = resp.json()["agents"]
    assert any(a["name"] == agent_name for a in agents)

    # Get
    resp = await client.get(f"/api/v1/agents/{agent_name}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == agent_name

    # Delete
    resp = await client.delete(f"/api/v1/agents/{agent_name}", headers=auth_headers)
    assert resp.status_code == 200

    # Verify deleted
    resp = await client.get(f"/api/v1/agents/{agent_name}", headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_agent_not_found(client, auth_headers):
    """Getting a nonexistent agent should return 404."""
    resp = await client.get("/api/v1/agents/nonexistent-agent-xyz", headers=auth_headers)
    assert resp.status_code == 404


# --- Network ---


@pytest.mark.anyio
async def test_network_interfaces(client, auth_headers):
    resp = await client.get("/api/v1/network/interfaces", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "interfaces" in data
    assert len(data["interfaces"]) > 0


@pytest.mark.anyio
async def test_network_connections(client, auth_headers):
    resp = await client.get("/api/v1/network/connections", headers=auth_headers)
    assert resp.status_code == 200
    assert "connections" in resp.json()


# --- WebOps ---


@pytest.mark.anyio
async def test_webops_services(client, auth_headers):
    resp = await client.get("/api/v1/webops/services", headers=auth_headers)
    assert resp.status_code == 200
    services = resp.json()["services"]
    assert "vercel" in services
    assert "github" in services


# --- SSH ---


@pytest.mark.anyio
async def test_ssh_sessions_empty(client, auth_headers):
    resp = await client.get("/api/v1/ssh/sessions", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["sessions"] == []


# --- Cross-cutting: error handling ---


@pytest.mark.anyio
async def test_nonexistent_route_returns_404(client, auth_headers):
    resp = await client.get("/api/v1/nonexistent", headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_health_does_not_require_auth(client):
    """Health and ready endpoints must be unauthenticated."""
    h = await client.get("/health")
    r = await client.get("/ready")
    assert h.status_code == 200
    assert r.status_code == 200
