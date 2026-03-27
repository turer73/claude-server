"""WebSocket connection status API."""

from __future__ import annotations

from fastapi import APIRouter

from app.ws.connection_manager import ConnectionManager

router = APIRouter(prefix="/api/v1/ws", tags=["websocket"])

# Global connection manager
ws_manager = ConnectionManager()


@router.get("/status")
async def ws_status():
    return ws_manager.get_stats()
