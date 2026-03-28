"""Log management REST API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.core.log_manager import LogManager
from app.middleware.dependencies import require_auth

router = APIRouter(prefix="/api/v1/logs", tags=["logs"])

_DEFAULT_SOURCES = {
    "agent-runner": "/var/log/linux-ai-server/agent-runner.log",
    "alerts": "/var/log/linux-ai-server/alerts.log",
    "health-check": "/var/log/linux-ai-server/health-check.log",
    "backup": "/var/log/linux-ai-server/backup.log",
}


def get_log_manager() -> LogManager:
    return LogManager(sources=_DEFAULT_SOURCES)


@router.get("/sources", dependencies=[Depends(require_auth)])
async def list_sources(lm: LogManager = Depends(get_log_manager)):
    return {"sources": lm.list_sources()}


@router.get("/tail", dependencies=[Depends(require_auth)])
async def tail_logs(
    source: str | None = None,
    n: int = Query(default=50, ge=1, le=1000),
    lm: LogManager = Depends(get_log_manager),
):
    return {"lines": lm.tail(source=source, n=n)}


@router.get("/search", dependencies=[Depends(require_auth)])
async def search_logs(
    pattern: str,
    source: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    lm: LogManager = Depends(get_log_manager),
):
    return {"results": lm.search(pattern, source=source, limit=limit)}


@router.get("/stats", dependencies=[Depends(require_auth)])
async def log_stats(lm: LogManager = Depends(get_log_manager)):
    return lm.stats()


@router.get("/journal", dependencies=[Depends(require_auth)])
async def journal_logs(
    unit: str = "linux-ai-server",
    n: int = Query(default=50, ge=1, le=500),
):
    """Read systemd journal logs for a service unit."""
    import asyncio

    allowed_units = {"linux-ai-server", "ollama", "docker", "nginx", "ssh", "cron"}
    if unit not in allowed_units:
        return {"error": f"Unit not allowed. Choose from: {allowed_units}"}

    proc = await asyncio.create_subprocess_exec(
        "journalctl", "-u", unit, "--no-pager", "-n", str(n), "--output=short-iso",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    lines = stdout.decode(errors="replace").strip().split("\n")
    return {"unit": unit, "lines": lines}
