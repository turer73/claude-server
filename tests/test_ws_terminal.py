"""Tests for WebSocket terminal handler."""

import pytest
import json
from unittest.mock import patch, AsyncMock, MagicMock
from httpx import AsyncClient, ASGITransport
from starlette.testclient import TestClient

from tests.conftest import TEST_API_KEY


@pytest.mark.anyio
async def test_ws_terminal_session_lifecycle(app, tmp_path, monkeypatch):
    """Test terminal WebSocket creates session and handles commands."""
    from app.db.database import Database
    from app.auth.api_key import hash_api_key

    db = Database(str(tmp_path / "test.db"))
    await db.initialize()
    await db.execute(
        "INSERT INTO api_keys (key_hash, name, permissions) VALUES (?, ?, ?)",
        (hash_api_key(TEST_API_KEY), "admin", "admin"),
    )
    app.state.db = db

    mock_session = MagicMock()
    mock_session.execute = AsyncMock(return_value={"stdout": "hello\n", "stderr": "", "exit_code": 0})
    mock_session.set_cwd = MagicMock()

    mock_mgr = MagicMock()
    mock_mgr.create_session.return_value = "test-sid-123"
    mock_mgr.get_session.return_value = mock_session
    mock_mgr.destroy_session = MagicMock()

    with patch("app.ws.terminal._terminal_mgr", mock_mgr):
        client = TestClient(app)
        with client.websocket_connect("/ws/terminal") as ws:
            # Should receive session_created
            data = ws.receive_json()
            assert data["type"] == "session_created"
            assert data["session_id"] == "test-sid-123"

            # Send command
            ws.send_text(json.dumps({"type": "command", "command": "echo hello"}))
            data = ws.receive_json()
            assert data["type"] == "output"
            assert data["stdout"] == "hello\n"

            # Send resize (should be ignored)
            ws.send_text(json.dumps({"type": "resize", "cols": 80, "rows": 24}))

            # Send cwd change
            ws.send_text(json.dumps({"type": "cwd", "path": "/tmp"}))
            data = ws.receive_json()
            assert data["type"] == "cwd_set"

    await db.close()
