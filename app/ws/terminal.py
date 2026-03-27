"""WebSocket endpoint for interactive terminal."""

from __future__ import annotations

import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.terminal_manager import TerminalManager

router = APIRouter()
_terminal_mgr = TerminalManager(max_sessions=5)


@router.websocket("/ws/terminal")
async def ws_terminal(websocket: WebSocket):
    await websocket.accept()
    sid = _terminal_mgr.create_session()
    session = _terminal_mgr.get_session(sid)

    try:
        await websocket.send_json({"type": "session_created", "session_id": sid})
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            command = msg.get("command", "")
            if msg.get("type") == "resize":
                continue  # resize handled by tmux when available
            if msg.get("type") == "cwd":
                session.set_cwd(msg.get("path", "/"))
                await websocket.send_json({"type": "cwd_set", "path": msg.get("path")})
                continue
            result = await session.execute(command)
            await websocket.send_json({"type": "output", **result})
    except WebSocketDisconnect:
        _terminal_mgr.destroy_session(sid)
    except Exception:
        _terminal_mgr.destroy_session(sid)
        await websocket.close()
