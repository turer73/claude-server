"""Tests for Claude Code API — status, run, sessions."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.anyio
async def test_claude_status_available(client, auth_headers):
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (b"1.0.0\n", b"")

    with (
        patch("app.api.claude_code._find_claude", return_value="/usr/bin/claude"),
        patch("app.api.claude_code._load_claude_token", return_value="test-token"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        resp = await client.get("/api/v1/claude/status", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert "1.0.0" in data["version"]


@pytest.mark.anyio
async def test_claude_status_not_available(client, auth_headers):
    with patch("app.api.claude_code._find_claude", return_value=None):
        resp = await client.get("/api/v1/claude/status", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["available"] is False


@pytest.mark.anyio
async def test_claude_run_no_binary(client, auth_headers):
    with patch("app.api.claude_code._find_claude", return_value=None):
        resp = await client.post("/api/v1/claude/run", json={"prompt": "hello"}, headers=auth_headers)
        assert resp.status_code == 200
        assert "error" in resp.json()


@pytest.mark.anyio
async def test_claude_run_success(client, auth_headers):
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (
        b'[{"type":"result","session_id":"abc123","result":"Hello!","total_cost_usd":0.01,"is_error":false}]',
        b"",
    )
    mock_proc.kill = MagicMock()

    with (
        patch("app.api.claude_code._find_claude", return_value="/usr/bin/claude"),
        patch("app.api.claude_code._build_env", return_value={}),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        resp = await client.post("/api/v1/claude/run", json={"prompt": "hello"}, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["session_id"] == "abc123"
        assert data["cost"] == 0.01


@pytest.mark.anyio
async def test_claude_run_requires_admin(client, read_headers):
    resp = await client.post("/api/v1/claude/run", json={"prompt": "hello"}, headers=read_headers)
    assert resp.status_code in (401, 403)


@pytest.mark.anyio
async def test_claude_sessions_empty(client, auth_headers):
    with patch("os.path.isdir", return_value=False):
        resp = await client.get("/api/v1/claude/sessions", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["sessions"] == []


@pytest.mark.anyio
async def test_claude_run_with_session(client, auth_headers):
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (b'{"result":"continued","session_id":"abc123"}', b"")
    mock_proc.kill = MagicMock()

    with (
        patch("app.api.claude_code._find_claude", return_value="/usr/bin/claude"),
        patch("app.api.claude_code._build_env", return_value={}),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        resp = await client.post(
            "/api/v1/claude/run",
            json={
                "prompt": "continue",
                "session_id": "abc123",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["session_id"] == "abc123"


@pytest.mark.anyio
async def test_claude_ui(client, auth_headers):
    resp = await client.get("/api/v1/claude/ui", headers=auth_headers)
    # Should return HTML or 404
    assert resp.status_code in (200, 404)
