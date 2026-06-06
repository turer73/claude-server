"""DevOps Agent API — autonomous monitoring, anomaly detection, auto-remediation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.middleware.dependencies import require_admin, require_auth

router = APIRouter(prefix="/api/v1/devops", tags=["devops"])


def _get_agent(request: Request):
    """Get the DevOps agent from app state."""
    return getattr(request.app.state, "devops_agent", None)


@router.get("/status")
async def agent_status(request: Request, _: None = Depends(require_auth)) -> dict:
    """Get DevOps agent status — running state, check count, active alerts."""
    agent = _get_agent(request)
    if not agent:
        return {"running": False, "error": "Agent not initialized"}
    return agent.status


@router.get("/alerts")
async def active_alerts(request: Request, _: None = Depends(require_auth)) -> dict:
    """Get currently active (unresolved) alerts."""
    agent = _get_agent(request)
    if not agent:
        return {"alerts": []}
    return {"alerts": agent.active_alerts}


@router.get("/alerts/history")
async def alerts_history(
    request: Request,
    limit: int = 50,
    severity: str | None = None,
    _: None = Depends(require_auth),
) -> dict:
    """Get alert history from database."""
    agent = _get_agent(request)
    if not agent:
        return {"alerts": []}
    alerts = await agent.get_alerts_history(limit=limit, severity=severity)
    return {"alerts": alerts, "count": len(alerts)}


@router.get("/metrics/history")
async def metrics_history(
    request: Request,
    minutes: int = 30,
    _: None = Depends(require_auth),
) -> dict:
    """Get metrics history (last N minutes)."""
    agent = _get_agent(request)
    if not agent:
        return {"metrics": []}
    metrics = await agent.get_metrics_history(minutes=minutes)
    return {"metrics": metrics, "count": len(metrics)}


@router.get("/metrics/buffer")
async def metrics_buffer(request: Request, _: None = Depends(require_auth)) -> dict:
    """Get in-memory metrics buffer (last ~1 hour)."""
    agent = _get_agent(request)
    if not agent:
        return {"metrics": []}
    return {"metrics": agent.metrics_buffer, "count": len(agent.metrics_buffer)}


@router.get("/vps/latest")
async def vps_latest(request: Request, _: None = Depends(require_auth)) -> dict:
    """Latest VPS sample collected by the agent (in-memory, no DB round-trip)."""
    agent = _get_agent(request)
    if not agent:
        return {"vps": {}}
    return {"vps": agent.latest_vps}


@router.get("/vps/metrics/history")
async def vps_metrics_history(
    request: Request,
    minutes: int = 60,
    _: None = Depends(require_auth),
) -> dict:
    """Get persisted VPS metric history (last N minutes)."""
    agent = _get_agent(request)
    if not agent:
        return {"metrics": []}
    metrics = await agent.get_vps_metrics_history(minutes=minutes)
    return {"metrics": metrics, "count": len(metrics)}


@router.get("/remediation/log")
async def remediation_log(request: Request, _: None = Depends(require_auth)) -> dict:
    """Get remediation action history."""
    agent = _get_agent(request)
    if not agent:
        return {"remediations": []}
    return {"remediations": agent.remediation_history}


@router.get("/playbooks")
async def list_playbooks(_: None = Depends(require_auth)) -> dict:
    """List all defined remediation playbooks."""
    from app.core.config import get_settings
    from app.core.devops_agent import PLAYBOOKS

    settings = get_settings()
    return {
        "playbooks": {k: [s["desc"] for s in v] for k, v in PLAYBOOKS.items()},
        "critical_services": settings.monitor_critical_services,
        "critical_containers": settings.monitor_critical_containers,
        "vps_containers": settings.monitor_vps_containers,
    }


class ForceRemediateRequest(BaseModel):
    """Slice-2 [🔧 Uygula]: kullanıcı-onaylı manuel remediation tetikleme.
    source VEYA event_id (event_id -> events.source çözülür)."""

    source: str | None = None
    event_id: int | None = None


@router.post("/remediate/force")
async def force_remediate(req: ForceRemediateRequest, request: Request, _: None = Depends(require_admin)) -> dict:
    """Telegram [🔧 Uygula] -> playbook'u ELLE çalıştır (remediation_mode BYPASS;
    tıklama = açık insan-onayı). Auth: internal-key (admin scope) -> owner-chat
    kontrolü telegram_bot katmanında. event_id verilirse source DB'den çözülür."""
    agent = _get_agent(request)
    if not agent:
        raise HTTPException(503, "DevOps agent not initialized")
    source = req.source
    if not source and req.event_id is not None:
        db = getattr(request.app.state, "db", None)
        if not db:
            raise HTTPException(503, "db unavailable")
        row = await db.fetch_one("SELECT source FROM events WHERE id=?", (req.event_id,))
        if not row:
            raise HTTPException(404, "event not found")
        source = row["source"]
    if not source:
        raise HTTPException(400, "source or event_id required")
    return await agent.force_remediate(source)
