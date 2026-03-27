"""Monitoring REST API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.monitor_agent import MonitorAgent
from app.models.schemas import MetricsSnapshot, AlertConfig

router = APIRouter(prefix="/api/v1/monitor", tags=["monitor"])


def get_monitor() -> MonitorAgent:
    return MonitorAgent()


@router.get("/metrics", response_model=MetricsSnapshot)
async def current_metrics(monitor: MonitorAgent = Depends(get_monitor)):
    return monitor.collect_metrics()


@router.post("/alerts/check")
async def check_alerts(
    config: AlertConfig,
    monitor: MonitorAgent = Depends(get_monitor),
):
    metrics = monitor.collect_metrics()
    thresholds = {
        "cpu_percent": config.cpu_percent,
        "memory_percent": config.memory_percent,
        "disk_percent": config.disk_percent,
        "temperature_c": config.temperature_c,
    }
    alerts = monitor.check_alerts(metrics, thresholds)
    return {"alerts": alerts, "metrics": metrics}
