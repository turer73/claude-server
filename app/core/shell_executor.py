"""Shell executor — full shell access via bash with whitelist validation.

Security model: JWT admin auth is the gate. Once authenticated as admin,
full shell access is granted through bash -c for pipe, redirection, etc.
The whitelist validates the *first* command in the pipeline.
"""

from __future__ import annotations

import asyncio
import time

from app.exceptions import AuthorizationError, ShellExecutionError

# Dangerous patterns that are NEVER allowed regardless of auth
BLOCKED_PATTERNS = ["rm -rf /", "mkfs /dev/sd", "dd if=/dev/zero of=/dev/sd", ":(){", ":()", "forkbomb"]


class ShellExecutor:
    def __init__(self, whitelist: list[str]) -> None:
        self._whitelist = set(whitelist)

    def validate_command(self, command: str) -> bool:
        if not command or not command.strip():
            raise AuthorizationError("Empty command")

        # Block catastrophic commands
        for pattern in BLOCKED_PATTERNS:
            if pattern in command:
                raise AuthorizationError(f"Blocked dangerous pattern: {pattern!r}")

        # Validate the first command in the pipeline is whitelisted
        # Strip leading env vars like VAR=val, sudo, etc.
        stripped = command.strip()
        # Handle sudo prefix
        if stripped.startswith("sudo "):
            stripped = stripped[5:].strip()
        # Handle env var prefix like KEY=val cmd
        while "=" in stripped.split()[0] if stripped.split() else False:
            stripped = stripped.split(None, 1)[1] if " " in stripped else stripped

        base = stripped.split()[0].split("/")[-1]  # basename
        # Also check pipe targets aren't the only validation — first cmd matters
        if base not in self._whitelist and base not in ("sudo",):
            raise AuthorizationError(f"Command {base!r} not in whitelist")

        return True

    async def execute(
        self, command: str, timeout: int = 30, cwd: str | None = None
    ) -> dict:
        self.validate_command(command)

        start = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise ShellExecutionError(f"Command timed out after {timeout}s")
        except FileNotFoundError:
            raise ShellExecutionError(f"Command not found in shell")

        elapsed = (time.monotonic() - start) * 1000

        return {
            "exit_code": proc.returncode or 0,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "elapsed_ms": round(elapsed, 1),
        }
