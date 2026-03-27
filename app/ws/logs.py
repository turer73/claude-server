"""WebSocket endpoint for live log streaming."""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket):
    await websocket.accept()
    log_path = "/var/log/linux-ai-server/server.log"

    try:
        if not os.path.isfile(log_path):
            await websocket.send_json({"error": f"Log file not found: {log_path}"})
            await websocket.close()
            return

        with open(log_path, "r") as f:
            f.seek(0, 2)  # seek to end
            while True:
                line = f.readline()
                if line:
                    await websocket.send_json({"line": line.rstrip()})
                else:
                    await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    except Exception:
        await websocket.close()
