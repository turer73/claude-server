"""ConnectionManager — async WebSocket yönetimi testleri."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.ws.connection_manager import ConnectionManager, ConnectionInfo


@pytest.fixture
def manager():
    return ConnectionManager()


def _mock_ws():
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    return ws


@pytest.mark.asyncio
async def test_connect(manager):
    ws = _mock_ws()
    await manager.connect("c1", ws, endpoint="/ws/terminal")
    assert manager.active_count() == 1
    ws.accept.assert_awaited_once()


@pytest.mark.asyncio
async def test_connect_tracks_total(manager):
    ws1 = _mock_ws()
    ws2 = _mock_ws()
    await manager.connect("c1", ws1)
    await manager.connect("c2", ws2)
    stats = manager.get_stats()
    assert stats["total_connected"] == 2
    assert stats["active_connections"] == 2


@pytest.mark.asyncio
async def test_disconnect(manager):
    ws = _mock_ws()
    await manager.connect("c1", ws)
    manager.disconnect("c1")
    assert manager.active_count() == 0
    stats = manager.get_stats()
    assert stats["total_disconnected"] == 1


def test_disconnect_nonexistent(manager):
    """Olmayan connection disconnect — hata vermemeli."""
    manager.disconnect("ghost")
    assert manager._total_disconnected == 1


@pytest.mark.asyncio
async def test_send_ping(manager):
    ws = _mock_ws()
    await manager.connect("c1", ws)
    await manager.send_ping("c1")
    ws.send_json.assert_awaited_once()
    args = ws.send_json.call_args[0][0]
    assert args["type"] == "ping"
    assert "timestamp" in args


@pytest.mark.asyncio
async def test_send_ping_nonexistent(manager):
    """Olmayan connection'a ping — hata vermemeli."""
    await manager.send_ping("ghost")


@pytest.mark.asyncio
async def test_send_ping_failure_disconnects(manager):
    ws = _mock_ws()
    ws.send_json.side_effect = Exception("connection lost")
    await manager.connect("c1", ws)
    await manager.send_ping("c1")
    assert manager.active_count() == 0


@pytest.mark.asyncio
async def test_handle_pong(manager):
    ws = _mock_ws()
    await manager.connect("c1", ws)
    await manager.handle_pong("c1")
    conn = manager._connections["c1"]
    assert conn.last_ping is not None


@pytest.mark.asyncio
async def test_handle_pong_nonexistent(manager):
    await manager.handle_pong("ghost")


@pytest.mark.asyncio
async def test_get_stats_with_connections(manager):
    ws = _mock_ws()
    await manager.connect("c1", ws, endpoint="/ws/monitor")
    stats = manager.get_stats()
    assert len(stats["connections"]) == 1
    assert stats["connections"][0]["id"] == "c1"
    assert stats["connections"][0]["endpoint"] == "/ws/monitor"


@pytest.mark.asyncio
async def test_broadcast(manager):
    ws1 = _mock_ws()
    ws2 = _mock_ws()
    await manager.connect("c1", ws1)
    await manager.connect("c2", ws2)
    await manager.broadcast({"type": "update", "data": 42})
    ws1.send_json.assert_awaited_once()
    ws2.send_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_broadcast_removes_dead(manager):
    ws1 = _mock_ws()
    ws2 = _mock_ws()
    ws2.send_json.side_effect = Exception("dead")
    await manager.connect("c1", ws1)
    await manager.connect("c2", ws2)
    await manager.broadcast({"msg": "test"})
    assert manager.active_count() == 1
    assert "c1" in manager._connections


@pytest.mark.asyncio
async def test_broadcast_empty(manager):
    """Bağlantı yokken broadcast — hata vermemeli."""
    await manager.broadcast({"msg": "hello"})
