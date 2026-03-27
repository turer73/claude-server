"""Terminal manager -- WebSocket interactive terminal sessions."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

from app.exceptions import NotFoundError, RateLimitError


class TerminalSession:
    """A single terminal session backed by subprocess."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.created_at = datetime.now().isoformat()
        self._cwd: str | None = None

    async def execute(self, command: str, timeout: int = 30) -> dict:
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            return {
                "exit_code": proc.returncode or 0,
                "stdout": stdout.decode(errors="replace"),
                "stderr": stderr.decode(errors="replace"),
            }
        except asyncio.TimeoutError:
            proc.kill()
            return {"exit_code": -1, "stdout": "", "stderr": f"Timeout after {timeout}s"}

    def set_cwd(self, cwd: str) -> None:
        self._cwd = cwd


class TerminalManager:
    """Manage multiple terminal sessions."""

    def __init__(self, max_sessions: int = 5) -> None:
        self._max = max_sessions
        self._sessions: dict[str, TerminalSession] = {}

    def create_session(self) -> str:
        if len(self._sessions) >= self._max:
            raise RateLimitError(f"Max terminal sessions ({self._max}) reached")
        sid = str(uuid.uuid4())[:8]
        self._sessions[sid] = TerminalSession(sid)
        return sid

    def get_session(self, session_id: str) -> TerminalSession:
        session = self._sessions.get(session_id)
        if not session:
            raise NotFoundError(f"Terminal session {session_id} not found")
        return session

    def destroy_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def list_sessions(self) -> list[dict]:
        return [
            {"id": sid, "created_at": s.created_at}
            for sid, s in self._sessions.items()
        ]

    def count(self) -> int:
        return len(self._sessions)

    def close_all(self) -> None:
        self._sessions.clear()
