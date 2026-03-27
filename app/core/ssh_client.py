"""SSH client — connect to remote servers, execute commands, transfer files."""

from __future__ import annotations

import uuid
from datetime import datetime

import paramiko

from app.exceptions import NotFoundError, RateLimitError, ShellExecutionError


class SSHClient:
    """Paramiko-based SSH client wrapper."""

    def connect(
        self,
        host: str,
        username: str,
        password: str | None = None,
        port: int = 22,
        key_path: str | None = None,
        timeout: int = 10,
    ) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=host,
                username=username,
                password=password,
                port=port,
                key_filename=key_path,
                timeout=timeout,
            )
            return client
        except Exception as e:
            raise ShellExecutionError(f"SSH connection failed: {e}")

    def exec_command(
        self,
        client: paramiko.SSHClient,
        command: str,
        timeout: int = 30,
    ) -> dict:
        try:
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            exit_code = stdout.channel.recv_exit_status()
            return {
                "exit_code": exit_code,
                "stdout": stdout.read().decode(errors="replace"),
                "stderr": stderr.read().decode(errors="replace"),
            }
        except Exception as e:
            raise ShellExecutionError(f"SSH command failed: {e}")

    def upload_file(
        self,
        client: paramiko.SSHClient,
        local_path: str,
        remote_path: str,
    ) -> bool:
        try:
            sftp = client.open_sftp()
            sftp.put(local_path, remote_path)
            sftp.close()
            return True
        except Exception as e:
            raise ShellExecutionError(f"SFTP upload failed: {e}")

    def download_file(
        self,
        client: paramiko.SSHClient,
        remote_path: str,
        local_path: str,
    ) -> bool:
        try:
            sftp = client.open_sftp()
            sftp.get(remote_path, local_path)
            sftp.close()
            return True
        except Exception as e:
            raise ShellExecutionError(f"SFTP download failed: {e}")


class SSHSessionManager:
    """Manage active SSH sessions."""

    def __init__(self, max_sessions: int = 5) -> None:
        self._max = max_sessions
        self._sessions: dict[str, dict] = {}

    def add(self, host: str, username: str, client: paramiko.SSHClient) -> str:
        if len(self._sessions) >= self._max:
            raise RateLimitError(f"Max SSH sessions ({self._max}) reached")
        sid = str(uuid.uuid4())[:8]
        self._sessions[sid] = {
            "host": host,
            "username": username,
            "client": client,
            "connected_at": datetime.now().isoformat(),
        }
        return sid

    def get(self, session_id: str) -> paramiko.SSHClient:
        session = self._sessions.get(session_id)
        if not session:
            raise NotFoundError(f"SSH session {session_id} not found")
        return session["client"]

    def remove(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session:
            session["client"].close()

    def list_sessions(self) -> list[dict]:
        return [
            {"id": sid, "host": s["host"], "username": s["username"], "connected_at": s["connected_at"]}
            for sid, s in self._sessions.items()
        ]

    def count(self) -> int:
        return len(self._sessions)

    def close_all(self) -> None:
        for sid in list(self._sessions.keys()):
            self.remove(sid)
