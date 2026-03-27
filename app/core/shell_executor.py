"""Shell executor — whitelisted command execution with injection prevention."""

from __future__ import annotations

import asyncio
import shlex
import time

from app.exceptions import AuthorizationError, ShellExecutionError

INJECTION_PATTERNS = [";", "&&", "||", "|", "`", "$(", "${", ">", "<", "\n"]


class ShellExecutor:
    def __init__(self, whitelist: list[str]) -> None:
        self._whitelist = set(whitelist)

    def validate_command(self, command: str) -> bool:
        if not command or not command.strip():
            raise AuthorizationError("Empty command")

        # Check injection patterns
        for pattern in INJECTION_PATTERNS:
            if pattern in command:
                raise AuthorizationError(f"Command injection detected: {pattern!r}")

        # Extract base command
        base = command.strip().split()[0]
        if base not in self._whitelist:
            raise AuthorizationError(f"Command {base!r} not in whitelist")

        return True

    async def execute(
        self, command: str, timeout: int = 30, cwd: str | None = None
    ) -> dict:
        self.validate_command(command)

        parts = shlex.split(command)
        start = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_exec(
                *parts,
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
            raise ShellExecutionError(f"Command not found: {parts[0]}")

        elapsed = (time.monotonic() - start) * 1000

        return {
            "exit_code": proc.returncode or 0,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "elapsed_ms": round(elapsed, 1),
        }
