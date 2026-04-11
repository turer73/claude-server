"""Webhook endpoints for n8n and external automation."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel

# No prefix here — will be mounted via monitoring router with explicit prefix
router = APIRouter(tags=["webhooks"])


class WebhookEvent(BaseModel):
    source: str
    event: str
    data: dict | None = None


class WebhookResponse(BaseModel):
    received: bool
    event_id: str
    timestamp: str


# In-memory event store (last 100 events)
_events: list[dict] = []
MAX_EVENTS = 100


@router.post("/receive", response_model=WebhookResponse)
async def receive_webhook(event: WebhookEvent):
    """Receive webhook from n8n or external sources."""
    event_id = str(uuid.uuid4())[:8]
    entry = {
        "event_id": event_id,
        "source": event.source,
        "event": event.event,
        "data": event.data,
        "timestamp": datetime.now().isoformat(),
    }
    _events.append(entry)
    if len(_events) > MAX_EVENTS:
        _events.pop(0)
    return WebhookResponse(
        received=True,
        event_id=event_id,
        timestamp=entry["timestamp"],
    )


@router.get("/events")
async def list_events(limit: int = 20):
    """List recent webhook events."""
    return {"events": _events[-limit:]}


@router.post("/trigger/{action}")
async def trigger_action(action: str, request: Request):
    """Trigger predefined actions (for n8n workflow integration).

    Supported actions:
    - health_check: Run health check and return status
    - metrics_snapshot: Get current system metrics
    - backup_create: Create a backup
    - alert_check: Check alert thresholds
    """
    from app.core.monitor_agent import MonitorAgent

    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass

    if action == "health_check":
        monitor = MonitorAgent()
        metrics = monitor.collect_metrics()
        return {
            "action": "health_check",
            "healthy": metrics["cpu_percent"] < 95 and metrics["memory_percent"] < 95,
            "metrics": metrics,
        }

    elif action == "metrics_snapshot":
        monitor = MonitorAgent()
        return {"action": "metrics_snapshot", "metrics": monitor.collect_metrics()}

    elif action == "alert_check":
        monitor = MonitorAgent()
        metrics = monitor.collect_metrics()
        thresholds = body.get("thresholds", {
            "cpu_percent": 85,
            "memory_percent": 85,
            "disk_percent": 90,
            "temperature_c": 80,
        })
        alerts = monitor.check_alerts(metrics, thresholds)
        return {
            "action": "alert_check",
            "alerts": alerts,
            "has_alerts": len(alerts) > 0,
            "metrics": metrics,
        }

    elif action == "backup_create":
        from app.core.backup_manager import BackupManager

        bm = BackupManager(
            source_dirs=["/var/AI-stump/"],
            backup_dir="/var/lib/linux-ai-server/backups",
        )
        try:
            result = bm.create_backup(label=body.get("label", "n8n"))
            return {"action": "backup_create", **result}
        except Exception as e:
            return {"action": "backup_create", "success": False, "error": str(e)}

    elif action == "verify_fix":
        monitor = MonitorAgent()
        metrics = monitor.collect_metrics()
        alert_source = body.get("alert_source", "")
        thresholds = {"cpu": 85, "memory": 85, "disk": 90, "temperature": 80}

        fixed = False
        detail = ""

        if alert_source in ("cpu", "memory", "disk", "temperature"):
            key_map = {
                "cpu": "cpu_percent",
                "memory": "memory_percent",
                "disk": "disk_percent",
                "temperature": "temperature",
            }
            key = key_map[alert_source]
            current = metrics.get(key, 0) or 0
            threshold = thresholds[alert_source]
            fixed = current < threshold
            detail = f"{alert_source}: {current:.1f}% (esik: {threshold}%)"
        elif alert_source.startswith("service:"):
            import subprocess
            svc = alert_source.split(":", 1)[1]
            try:
                r = subprocess.run(
                    ["systemctl", "is-active", svc],
                    capture_output=True, text=True, timeout=5,
                )
                fixed = r.stdout.strip() == "active"
                detail = f"Servis {svc}: {'active' if fixed else r.stdout.strip()}"
            except Exception as e:
                detail = f"Servis kontrol hatasi: {e}"
        elif alert_source.startswith("docker:"):
            import subprocess
            container = alert_source.split(":", 1)[1]
            try:
                r = subprocess.run(
                    ["docker", "ps", "--filter", f"name={container}", "--format", "{{.Status}}"],
                    capture_output=True, text=True, timeout=5,
                )
                fixed = "Up" in r.stdout
                detail = f"Container {container}: {r.stdout.strip() or 'not running'}"
            except Exception as e:
                detail = f"Docker kontrol hatasi: {e}"
        else:
            # Genel saglik kontrolu
            fixed = metrics["cpu_percent"] < 95 and metrics["memory_percent"] < 95
            detail = "Genel saglik kontrolu"

        return {
            "action": "verify_fix",
            "alert_source": alert_source,
            "fixed": fixed,
            "detail": detail,
            "metrics": metrics,
            "honest_assessment": "Sorun cozuldu" if fixed else "Sorun devam ediyor",
        }

    else:
        return {
            "error": f"Unknown action: {action}",
            "available": [
                "health_check",
                "metrics_snapshot",
                "alert_check",
                "backup_create",
                "verify_fix",
            ],
        }
