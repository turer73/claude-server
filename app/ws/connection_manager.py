"""WebSocket connection manager with heartbeat tracking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import WebSocket


@dataclass
class ConnectionInfo:
    websocket: WebSocket
    connected_at: str
    last_ping: str | None = None
    endpoint: str = ""


class ConnectionManager:
    """Track and manage WebSocket connections with heartbeat."""

    def __init__(self) -> None:
        self._connections: dict[str, ConnectionInfo] = {}
        self._total_connected: int = 0
        self._total_disconnected: int = 0

    def active_count(self) -> int:
        return len(self._connections)

    async def connect(self, conn_id: str, websocket: WebSocket, endpoint: str = "") -> None:
        await websocket.accept()
        self._connections[conn_id] = ConnectionInfo(
            websocket=websocket,
            connected_at=datetime.now(UTC).isoformat(),
            endpoint=endpoint,
        )
        self._total_connected += 1

    def disconnect(self, conn_id: str) -> None:
        self._connections.pop(conn_id, None)
        self._total_disconnected += 1

    async def send_ping(self, conn_id: str) -> None:
        conn = self._connections.get(conn_id)
        if conn:
            now = datetime.now(UTC).isoformat()
            conn.last_ping = now
            try:
                await conn.websocket.send_json({"type": "ping", "timestamp": now})
            except Exception:
                self.disconnect(conn_id)

    async def handle_pong(self, conn_id: str) -> None:
        conn = self._connections.get(conn_id)
        if conn:
            conn.last_ping = datetime.now(UTC).isoformat()

    def get_stats(self) -> dict:
        return {
            "active_connections": self.active_count(),
            "total_connected": self._total_connected,
            "total_disconnected": self._total_disconnected,
            "connections": [
                {
                    "id": cid,
                    "endpoint": c.endpoint,
                    "connected_at": c.connected_at,
                    "last_ping": c.last_ping,
                }
                for cid, c in self._connections.items()
            ],
        }

    async def broadcast(self, message: dict) -> None:
        dead = []
        for conn_id, conn in self._connections.items():
            try:
                await conn.websocket.send_json(message)
            except Exception:
                dead.append(conn_id)
        for conn_id in dead:
            self.disconnect(conn_id)
