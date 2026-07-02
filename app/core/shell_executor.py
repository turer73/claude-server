"""Shell executor — TAM-SHELL admin araci (whitelist DEGIL).

GUVENLIK MODELI (durust): Bu endpoint authenticated admin'e TAM bash erisimi
verir (`create_subprocess_shell` -> pipe/redirect/chain/$()/ hepsi calisir).
ASIL guvenlik siniri = JWT/API-key ADMIN AUTH; admin ile sistem zaten tam-kontrol
(sudo NOPASSWD). Dashboard terminal + run-agent.sh bunu bilincli kullanir.

Bu modulde IKI katman var, ama ikisi de guvenlik-siniri DEGIL:
  1) _first_command_whitelisted: SADECE ilk komutu kontrol eder; `cat x; <her sey>`
     ile asilabilir (tam string shell'e gider). Bu bir yazim-hatasi/yanlislik
     suzgeci; kotu-niyetli admin'e karsi koruma SAGLAMAZ.
  2) _DANGEROUS_PATTERNS: katastrofik komutlari (rm -rf /, mkfs/dd ham-device,
     fork-bomb, ...) engeller. AMAC: KAZA onleme (typo/yanlis-path), bilincli
     bypass'a karsi degil — blocklist her zaman asilabilir. Tetiklenen her blok
     audit icin WARNING loglanir.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from app.exceptions import AuthorizationError, ShellExecutionError

logger = logging.getLogger(__name__)

# Geriye-donuk: eski isim disariya referansli olabilir (test/doc).
BLOCKED_PATTERNS = ["rm -rf /", "mkfs /dev/sd", "dd if=/dev/zero of=/dev/sd", ":(){", ":()", "forkbomb"]

# rm'in recursive+force bayrak kombinasyonlari (sira serbest).
_RM_RF = (
    r"rm\s+(?:-\S*r\S*f\S*|-\S*f\S*r\S*|-[rR]\s+-f\S*|-f\S*\s+-[rR]"
    r"|--recursive\s+--force|--force\s+--recursive|-[rR]\s+--force|--recursive\s+-f\S*)"
)
# Katastrofik hedef = kok, glob, ~/$HOME veya TUM sistem-dizini (alt-path DEGIL).
_SYS_TARGET = r"(?:/|/\*|~|\$HOME|/(?:etc|usr|bin|sbin|lib\w*|boot|var|home|root|opt|dev|proc|sys)(?:/\*?)?)(?:\s|$)"

# (regex, etiket) — KAZA onleme amacli; guvenlik-siniri degil (auth odur).
_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(_RM_RF + r"\s+" + _SYS_TARGET), "rm -rf sistem-dizini/kok"),
    (re.compile(r"mkfs(?:\.\w+)?\b[^;&|]*?/dev/(?:sd|nvme|vd|hd|mmcblk)"), "mkfs ham-device"),
    (re.compile(r"\bdd\b[^;&|]*\bof=/dev/(?:sd|nvme|vd|hd|mmcblk)"), "dd ham-device"),
    (re.compile(r"\b(?:wipefs|shred)\b[^;&|]*?/dev/(?:sd|nvme|vd|hd|mmcblk)"), "wipefs/shred device"),
    (re.compile(r">\s*/dev/(?:sd|nvme|vd|hd|mmcblk)"), "block-device redirect"),
    (
        # rm guard'i ile ayni hedef-siniri: SADECE tum sistem-dizini/kok (alt-path
        # legit: `chmod -R 755 /opt/.../proje`, `chown -R u /home/u/x` ENGELLENMEZ).
        re.compile(r"ch(?:mod|own)\s+(?:-\S*R\S*|--recursive)\s+\S+\s+" + _SYS_TARGET),
        "chmod/chown -R sistem-dizini/kok",
    ),
    (re.compile(r":\s*\(\s*\)\s*\{"), "fork bomb"),
]


class ShellExecutor:
    def __init__(self, whitelist: list[str]) -> None:
        self._whitelist = set(whitelist)

    def validate_command(self, command: str) -> bool:
        if not command or not command.strip():
            raise AuthorizationError("Empty command")

        # Whitespace-normalize: `rm  -rf   /` gibi kacamaklar yakalansin.
        normalized = re.sub(r"\s+", " ", command.strip())

        # Katmanlardan biri: katastrofik-komut blok (kaza onleme; auth degil).
        for pattern, label in _DANGEROUS_PATTERNS:
            if pattern.search(normalized):
                logger.warning("shell blocked dangerous command (%s): %r", label, command[:200])
                raise AuthorizationError(f"Blocked dangerous pattern: {label}")

        # Soft suzgec: ilk komut whitelist'te mi (typo/yanlislik; bypass edilebilir).
        stripped = normalized
        if stripped.startswith("sudo "):
            stripped = stripped[5:].strip()
        # `KEY=val cmd` env-var prefix'lerini atla
        while stripped.split() and "=" in stripped.split()[0]:
            parts = stripped.split(None, 1)
            if len(parts) < 2:
                break
            stripped = parts[1]

        base = stripped.split()[0].split("/")[-1]  # basename
        if base not in self._whitelist and base != "sudo":
            raise AuthorizationError(f"Command {base!r} not in whitelist")

        return True

    async def execute(self, command: str, timeout: int = 30, cwd: str | None = None) -> dict[str, Any]:
        self.validate_command(command)

        start = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()  # reap the killed child — kill() alone leaves a zombie
            raise ShellExecutionError(f"Command timed out after {timeout}s")
        except FileNotFoundError:
            raise ShellExecutionError("Command not found in shell")

        elapsed = (time.monotonic() - start) * 1000

        return {
            "exit_code": proc.returncode or 0,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "elapsed_ms": round(elapsed, 1),
        }
