"""Background task queue — async work that runs without blocking the API.

Tasks are stored in SQLite jobs table and processed by a background worker.
Supports: shell commands, VPS commands, deploy triggers, scheduled tasks.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.shell_executor import ShellExecutor
from app.core.config import get_settings


@dataclass
class TaskResult:
    task_id: int
    type: str
    status: str  # pending, running, completed, failed
    result: str
    elapsed_ms: float


class TaskQueue:
    """Background task processor with SQLite-backed queue."""

    def __init__(self, db=None) -> None:
        self._db = db
        self._running = False
        self._task: asyncio.Task | None = None
        self._processed = 0
        self._recent: deque[TaskResult] = deque(maxlen=50)
        settings = get_settings()
        self._executor = ShellExecutor(whitelist=settings.shell_whitelist)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    @property
    def status(self) -> dict:
        return {
            "running": self._running,
            "processed": self._processed,
            "recent_count": len(self._recent),
        }

    @property
    def recent_tasks(self) -> list[dict]:
        return [
            {"task_id": r.task_id, "type": r.type, "status": r.status,
             "result": r.result[:200], "elapsed_ms": r.elapsed_ms}
            for r in reversed(self._recent)
        ]

    async def enqueue(self, task_type: str, payload: dict) -> int:
        """Add a task to the queue. Returns task ID."""
        if not self._db:
            return -1
        await self._db.execute(
            "INSERT INTO jobs (type, payload, status, created_at) VALUES (?, ?, 'pending', ?)",
            (task_type, json.dumps(payload), datetime.now(timezone.utc).isoformat()),
        )
        rows = await self._db.fetch_all("SELECT id FROM jobs ORDER BY id DESC LIMIT 1")
        return rows[0]["id"] if rows else -1

    async def get_task(self, task_id: int) -> dict | None:
        """Get a task by ID."""
        if not self._db:
            return None
        rows = await self._db.fetch_all("SELECT * FROM jobs WHERE id = ?", (task_id,))
        return dict(rows[0]) if rows else None

    async def list_pending(self) -> list[dict]:
        if not self._db:
            return []
        rows = await self._db.fetch_all(
            "SELECT id, type, status, created_at FROM jobs WHERE status IN ('pending', 'running') ORDER BY id"
        )
        return [dict(r) for r in rows]

    # ── Worker Loop ──────────────────────────────

    async def _worker_loop(self) -> None:
        while self._running:
            try:
                await self._process_next()
            except Exception:
                pass
            await asyncio.sleep(2)  # Poll every 2s

    async def _process_next(self) -> None:
        if not self._db:
            return

        # Fetch oldest pending job
        rows = await self._db.fetch_all(
            "SELECT * FROM jobs WHERE status = 'pending' ORDER BY id ASC LIMIT 1"
        )
        if not rows:
            return

        job = dict(rows[0])
        job_id = job["id"]
        job_type = job["type"]
        payload = json.loads(job.get("payload", "{}"))

        # Mark as running
        await self._db.execute(
            "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), job_id),
        )

        start = time.monotonic()
        result_text = ""
        success = True

        try:
            if job_type == "shell":
                r = await self._executor.execute(payload.get("command", "echo noop"), timeout=payload.get("timeout", 60))
                result_text = r.get("stdout", "") + r.get("stderr", "")
                success = r.get("exit_code", 1) == 0

            elif job_type == "vps_exec":
                vps_host = os.environ.get("VPS_HOST", "")
                cmd = f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 {vps_host} '{payload.get('command', 'echo noop')}'"
                r = await self._executor.execute(cmd, timeout=payload.get("timeout", 60))
                result_text = r.get("stdout", "") + r.get("stderr", "")
                success = r.get("exit_code", 1) == 0

            elif job_type == "deploy":
                r = await self._executor.execute(
                    "bash -c 'cd /opt/linux-ai-server && source venv/bin/activate && python -m pytest tests/ -q --ignore=tests/test_mcp.py 2>&1 | tail -5'",
                    timeout=120,
                )
                result_text = r.get("stdout", "")
                if r.get("exit_code", 1) == 0:
                    r2 = await self._executor.execute("systemctl restart linux-ai-server", timeout=15)
                    result_text += "\nRestarted: " + str(r2.get("exit_code", "?"))
                success = r.get("exit_code", 1) == 0

            elif job_type == "backup":
                r = await self._executor.execute(
                    "/opt/linux-ai-server/automation/daily-backup.sh", timeout=60
                )
                result_text = r.get("stdout", "")
                success = r.get("exit_code", 1) == 0

            else:
                result_text = f"Unknown job type: {job_type}"
                success = False

        except Exception as e:
            result_text = str(e)
            success = False

        elapsed = round((time.monotonic() - start) * 1000, 1)
        status = "completed" if success else "failed"

        await self._db.execute(
            "UPDATE jobs SET status = ?, completed_at = ?, result = ?, error = ? WHERE id = ?",
            (status, datetime.now(timezone.utc).isoformat(), result_text[:2000], "" if success else result_text[:500], job_id),
        )

        self._processed += 1
        self._recent.append(TaskResult(
            task_id=job_id, type=job_type, status=status,
            result=result_text[:200], elapsed_ms=elapsed,
        ))
