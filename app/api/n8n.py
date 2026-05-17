"""n8n workflow status — lokal n8n REST API'sini proxy'ler, dashboard widget'i icin.

Sadece read: aktif workflow listesi + son execution durumu + 24h fail sayisi.
Workflow tetiklemez, durdurmaz. Editor link'i n8n.panola.app uzerinden.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException

from app.core.config import read_env_var
from app.middleware.dependencies import require_auth

router = APIRouter(prefix="/api/v1/n8n", tags=["n8n"])

N8N_BASE = "http://localhost:5678"
EDITOR_BASE = "https://n8n.panola.app"


def _key() -> str:
    k = read_env_var("N8N_API_KEY")
    if not k:
        raise HTTPException(status_code=503, detail="N8N_API_KEY missing")
    return k


@router.get("/workflows-status")
async def workflows_status(_: None = Depends(require_auth)) -> dict[str, Any]:
    headers = {"X-N8N-API-KEY": _key()}
    async with httpx.AsyncClient(timeout=8.0, headers=headers) as c:
        wf = await c.get(f"{N8N_BASE}/api/v1/workflows?active=true&limit=50")
        ex = await c.get(f"{N8N_BASE}/api/v1/executions?limit=200")
    wf.raise_for_status()
    ex.raise_for_status()

    cutoff = datetime.now(UTC) - timedelta(hours=24)
    per: dict[str, dict[str, Any]] = {}
    for e in ex.json().get("data", []):
        wid = e.get("workflowId") or ""
        bucket = per.setdefault(wid, {"last": None, "fail24h": 0, "ok24h": 0})
        if bucket["last"] is None:
            bucket["last"] = {
                "status": e.get("status"),
                "startedAt": e.get("startedAt", ""),
            }
        try:
            dt = datetime.fromisoformat(e.get("startedAt", "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt < cutoff:
            continue
        st = e.get("status")
        if st == "error":
            bucket["fail24h"] += 1
        elif st == "success":
            bucket["ok24h"] += 1

    out: list[dict[str, Any]] = []
    for w in wf.json().get("data", []):
        wid = w.get("id", "")
        b = per.get(wid, {"last": None, "fail24h": 0, "ok24h": 0})
        out.append(
            {
                "id": wid,
                "name": w.get("name", ""),
                "active": w.get("active", False),
                "last": b["last"],
                "fail24h": b["fail24h"],
                "ok24h": b["ok24h"],
                "editorUrl": f"{EDITOR_BASE}/workflow/{wid}",
            }
        )
    out.sort(key=lambda x: (-x["fail24h"], x["name"].lower()))
    return {"workflows": out, "count": len(out)}
