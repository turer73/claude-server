"""Ops digest API — read-only JSON view backing the dashboard's
Operasyonlar tab and an explicit Telegram-send trigger.

Reuses app.core.digest so the CLI (automation/digest.py) and this
endpoint stay single-source. No writes to project codebases or DBs.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.core import digest as core_digest
from app.middleware.dependencies import require_auth

router = APIRouter(prefix="/api/v1/digest", tags=["digest"])


@router.get("/data")
async def digest_data(_: None = Depends(require_auth)) -> dict[str, Any]:
    """Gather the 24h digest as JSON. Reads claude_memory.db, public
    GitHub feeds (private repos require GITHUB_TOKEN in .env), local
    self-pentest logs, and host service/disk/RAM. ~3s wall time on klipper."""
    env = core_digest.load_env()
    data = core_digest.gather(token=env.get("GITHUB_TOKEN") or None)
    data["has_signal"] = core_digest.has_signal(data)
    return data


@router.post("/send")
async def digest_send(_: None = Depends(require_auth)) -> dict[str, Any]:
    """Render the digest as HTML and push to the configured Telegram
    chat. Honors the same NOTHING_NEW guard as the CLI — silent runs
    return {"sent": False, "reason": "NOTHING_NEW"} without hitting Telegram."""
    env = core_digest.load_env()
    data = core_digest.gather(token=env.get("GITHUB_TOKEN") or None)
    if not core_digest.has_signal(data):
        return {"sent": False, "reason": "NOTHING_NEW"}
    ok = core_digest.send_telegram(core_digest.render_html(data), env)
    return {"sent": ok, "reason": "ok" if ok else "telegram_failed"}
