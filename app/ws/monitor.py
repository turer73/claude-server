"""WebSocket endpoint for real-time metrics streaming."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.monitor_agent import MonitorAgent
from app.ws.auth import authenticate_ws

router = APIRouter()
_monitor = MonitorAgent()


@router.websocket("/ws/monitor")
async def ws_monitor(websocket: WebSocket):
    # GÜVENLIK: auth'suz accept metrik sızdırıyordu -> doğrula (read yeterli).
    if await authenticate_ws(websocket) is None:
        return
    await websocket.accept()
    try:
        while True:
            metrics = _monitor.collect_metrics()
            await websocket.send_json(metrics)
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        pass
    except Exception:
        await websocket.close()
