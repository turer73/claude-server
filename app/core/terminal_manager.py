"""Terminal manager -- WebSocket interactive terminal sessions.

Supports real PTY on Linux (interactive programs like vim, top, htop work).
Falls back to subprocess on Windows/non-PTY platforms.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime

from app.exceptions import NotFoundError, RateLimitError

# PTY support detection
_HAS_PTY = False
try:
    if sys.platform != "win32":
        import fcntl
        import pty
        import signal
        import struct
        import termios

        _HAS_PTY = True
except ImportError:
    pass


class TerminalSession:
    """A single terminal session — PTY-backed on Linux, subprocess fallback on Windows."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.created_at = datetime.now().isoformat()
        self._cwd: str | None = None
        self._pty_fd: int | None = None
        self._pid: int | None = None
        self._is_pty = False

    async def start_pty(self, cols: int = 80, rows: int = 24) -> None:
        """Start a real PTY session (Linux only)."""
        if not _HAS_PTY:
            return  # Fallback to per-command mode

        pid, fd = pty.openpty()
        shell = os.environ.get("SHELL", "/bin/bash")

        child_pid = os.fork()
        if child_pid == 0:
            # Child process — become the session leader
            os.setsid()
            # Open the slave side
            slave_fd = os.open(os.ttyname(pid), os.O_RDWR)
            os.close(pid)
            os.close(fd)
            # Redirect stdin/stdout/stderr
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)
            if self._cwd:
                os.chdir(self._cwd)
            os.execvp(shell, [shell, "-l"])
        else:
            os.close(pid)
            self._pty_fd = fd
            self._pid = child_pid
            self._is_pty = True
            # Set window size
            self.resize(cols, rows)
            # Make non-blocking
            import fcntl

            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def resize(self, cols: int, rows: int) -> None:
        """Resize the PTY terminal."""
        if self._is_pty and self._pty_fd is not None:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self._pty_fd, termios.TIOCSWINSZ, winsize)

    async def write_pty(self, data: str) -> None:
        """Write data to the PTY (keystrokes from client)."""
        if self._is_pty and self._pty_fd is not None:
            os.write(self._pty_fd, data.encode())

    async def read_pty(self, max_bytes: int = 4096) -> str:
        """Read available output from the PTY."""
        if not self._is_pty or self._pty_fd is None:
            return ""
        try:
            data = os.read(self._pty_fd, max_bytes)
            return data.decode(errors="replace")
        except (OSError, BlockingIOError):
            return ""

    async def execute(self, command: str, timeout: int = 30) -> dict:
        """Execute a command (subprocess fallback mode)."""
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return {
                "exit_code": proc.returncode or 0,
                "stdout": stdout.decode(errors="replace"),
                "stderr": stderr.decode(errors="replace"),
            }
        except TimeoutError:
            proc.kill()
            return {"exit_code": -1, "stdout": "", "stderr": f"Timeout after {timeout}s"}

    def set_cwd(self, cwd: str) -> None:
        self._cwd = cwd

    def close(self) -> None:
        """Clean up PTY resources."""
        if self._is_pty:
            if self._pty_fd is not None:
                try:
                    os.close(self._pty_fd)
                except OSError:
                    pass
                self._pty_fd = None
            if self._pid is not None:
                try:
                    os.kill(self._pid, signal.SIGTERM)
                    os.waitpid(self._pid, os.WNOHANG)
                except (OSError, ChildProcessError):
                    pass
                self._pid = None

    @property
    def is_pty(self) -> bool:
        return self._is_pty


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
        session = self._sessions.pop(session_id, None)
        if session:
            session.close()

    def list_sessions(self) -> list[dict]:
        return [{"id": sid, "created_at": s.created_at, "pty": s.is_pty} for sid, s in self._sessions.items()]

    def count(self) -> int:
        return len(self._sessions)

    def close_all(self) -> None:
        for session in self._sessions.values():
            session.close()
        self._sessions.clear()
