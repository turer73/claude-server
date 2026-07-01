"""WebSocket connection status API."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.memory import verify_key
from app.ws.connection_manager import ConnectionManager

router = APIRouter(prefix="/api/v1/ws", tags=["websocket"])

# Global connection manager
ws_manager = ConnectionManager()


@router.get("/status", dependencies=[Depends(verify_key)])
async def ws_status():
    return ws_manager.get_stats()
