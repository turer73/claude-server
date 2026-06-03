"""Tests for WebSocket terminal handler."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.auth.jwt_handler import create_token
from tests.conftest import TEST_API_KEY, TEST_JWT_SECRET

# ── GÜVENLIK REGRESYON: kimliksiz RCE açığının KAPALI olduğunu kilitle ──


def test_ws_terminal_rejects_without_token(app):
    """Token'sız /ws/terminal REDDEDILMELI (eski: kimliksiz RCE)."""
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/terminal") as ws:
            ws.receive_json()


def test_ws_terminal_rejects_non_admin_token(app):
    """read-perm token /ws/terminal'e YETMEMELI (terminal=admin-only shell)."""
    token = create_token(subject="r", permissions="read", secret=TEST_JWT_SECRET)
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/terminal?token={token}") as ws:
            ws.receive_json()


def test_ws_terminal_rejects_malformed_token(app):
    """Bozuk/geçersiz token TEMIZ 1008-reddi olmalı (500/trace DEĞİL) — decode_token
    AuthenticationError fırlatır, JWTError değil (Codex #26 bug fix)."""
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/terminal?token=not.a.valid.jwt") as ws:
            ws.receive_json()


@pytest.mark.anyio
async def test_ws_terminal_session_lifecycle(app, tmp_path, monkeypatch):
    """Test terminal WebSocket creates session and handles commands."""
    from app.auth.api_key import hash_api_key
    from app.db.database import Database

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

    # GÜVENLIK: /ws/terminal artık ADMIN-auth şart (kimliksiz RCE fix) -> token geç.
    token = create_token(subject="test-admin", permissions="admin", secret=TEST_JWT_SECRET)
    with patch("app.ws.terminal._terminal_mgr", mock_mgr):
        client = TestClient(app)
        with client.websocket_connect(f"/ws/terminal?token={token}") as ws:
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
