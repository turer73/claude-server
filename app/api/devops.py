"""DevOps Agent API — autonomous monitoring, anomaly detection, auto-remediation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.middleware.dependencies import require_auth

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
    from app.core.devops_agent import CRITICAL_CONTAINERS, CRITICAL_SERVICES, PLAYBOOKS

    return {
        "playbooks": {k: [s["desc"] for s in v] for k, v in PLAYBOOKS.items()},
        "critical_services": CRITICAL_SERVICES,
        "critical_containers": CRITICAL_CONTAINERS,
    }
