"""SSH client — connect to remote servers, execute commands, transfer files."""

from __future__ import annotations

import base64
import hashlib
import logging
import uuid
from datetime import datetime

import paramiko

from app.core.config import read_env_var
from app.exceptions import NotFoundError, RateLimitError, ShellExecutionError

log = logging.getLogger(__name__)


def _key_fingerprint(key: paramiko.PKey) -> str:
    """OpenSSH-uyumlu SHA256 fingerprint (SHA256:base64) — denetim-izi için."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


class _LogAndAcceptPolicy(paramiko.MissingHostKeyPolicy):
    """Bilinmeyen host: logla + kabul et (TOFU). paramiko.WarningPolicy gibi AMA
    `logging` kullanır, `warnings.warn` DEĞİL (Codex P2): WarningPolicy `PYTHONWARNINGS=error`
    veya error-filter altında bilinmeyen-host bağlantısını exception'a çevirip kırardı."""

    def missing_host_key(self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey) -> None:
        # FINGERPRINT logla (Codex P3): get_name() yalnız algoritma; log-and-accept'in TEK
        # denetim-izi bu → operatör sonraki key-değişimi/MITM ile karşılaştırabilsin.
        log.warning("SSH bilinmeyen host-key kabul edildi (TOFU): %s %s %s", hostname, key.get_name(), _key_fingerprint(key))


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
        # Host-key pinning (MITM sertleştirme): known_hosts'u yükle → BİLİNEN bir host'un
        # key'i değişirse paramiko BadHostKeyException atar (re-key veya aktif-MITM yakalanır;
        # eski AutoAddPolicy bunu sessiz-geçiyordu). Bilinmeyen-host politikası:
        #   default _LogAndAcceptPolicy = logla+bağlan (arbitrary-host SSH aracı kırılmaz),
        #   SSH_STRICT_HOST_KEY=1 → RejectPolicy = yalnız known_hosts'taki host'lar (opt-in sıkı).
        try:
            client.load_system_host_keys()
        except OSError:
            pass  # known_hosts yoksa sorun değil; politika devreye girer
        # read_env_var: os.environ + .env-dosyasi (systemd EnvironmentFile gecmiyor).
        # os.environ.get tek-basina .env'deki SSH_STRICT_HOST_KEY=1'i goremezdi -> guvenlik
        # gate'i serviste sessizce olu kalirdi (#174 sinifi; bkz app/core/dead_gate.py).
        strict = (read_env_var("SSH_STRICT_HOST_KEY") or "").strip().lower() in ("1", "true", "yes")
        client.set_missing_host_key_policy(paramiko.RejectPolicy() if strict else _LogAndAcceptPolicy())
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
