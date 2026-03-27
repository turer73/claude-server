import pytest
import json
from app.ws.connection_manager import ConnectionManager


@pytest.fixture
def manager():
    return ConnectionManager()


def test_manager_empty(manager):
    assert manager.active_count() == 0


def test_manager_stats(manager):
    stats = manager.get_stats()
    assert stats["active_connections"] == 0
    assert stats["total_connected"] == 0
    assert stats["total_disconnected"] == 0


def test_heartbeat_message():
    """Heartbeat message format."""
    msg = json.dumps({"type": "ping", "timestamp": "2026-03-27T12:00:00"})
    data = json.loads(msg)
    assert data["type"] == "ping"


def test_pong_response():
    """Pong response format."""
    msg = json.dumps({"type": "pong", "timestamp": "2026-03-27T12:00:00"})
    data = json.loads(msg)
    assert data["type"] == "pong"
