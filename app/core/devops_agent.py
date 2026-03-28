"""Autonomous DevOps Agent — monitors, detects anomalies, auto-remediates."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.core.monitor_agent import MonitorAgent
from app.core.shell_executor import ShellExecutor
from app.core.config import get_settings


@dataclass
class Alert:
    id: str
    severity: str  # warning, critical
    source: str  # cpu, memory, disk, temperature, service, docker
    message: str
    value: float
    threshold: float
    timestamp: str
    resolved: bool = False
    resolved_at: str | None = None
    remediation: str | None = None


@dataclass
class RemediationRecord:
    timestamp: str
    alert_source: str
    action: str
    command: str
    result: str
    success: bool


# ── Playbooks ──────────────────────────────────────────────

PLAYBOOKS: dict[str, list[dict]] = {
    "cpu_critical": [
        {"desc": "Log top CPU consumers", "cmd": "ps aux --sort=-%cpu | head -6"},
    ],
    "memory_critical": [
        {"desc": "Docker prune unused", "cmd": "docker system prune -f --volumes 2>/dev/null || true"},
        {"desc": "Clear pip cache", "cmd": "pip cache purge 2>/dev/null || true"},
        {"desc": "Clear tmp files", "cmd": "find /tmp -type f -mtime +1 -delete 2>/dev/null || true"},
    ],
    "disk_critical": [
        {"desc": "Docker prune", "cmd": "docker system prune -f 2>/dev/null || true"},
        {"desc": "Rotate logs", "cmd": "find /var/log -name '*.log' -size +50M -exec truncate -s 10M {} \\; 2>/dev/null || true"},
        {"desc": "Remove old backups", "cmd": "ls -t /data/backups/*.tar.gz 2>/dev/null | tail -n +4 | xargs rm -f 2>/dev/null || true"},
    ],
    "temperature_critical": [
        {"desc": "Set CPU governor to powersave", "cmd": "cpufreq-set -g powersave 2>/dev/null || echo 'powersave' | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null || true"},
    ],
    "service_down": [
        {"desc": "Restart service", "cmd": "systemctl restart {service}"},
    ],
    "docker_down": [
        {"desc": "Start container", "cmd": "docker start {container}"},
    ],
}

CRITICAL_SERVICES = ["linux-ai-server", "ollama"]
CRITICAL_CONTAINERS = ["n8n", "prometheus", "grafana", "chromadb", "paperless"]
VPS_HOST = "root@REDACTED_VPS_IP"
VPS_SSH = f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 {VPS_HOST}"
VPS_CONTAINERS = ["coolify", "panola-postgres", "panola-caddy", "n8n", "uptime-kuma", "plausible-plausible-1"]


class DevOpsAgent:
    """Background agent that collects metrics, detects anomalies, and auto-remediates."""

    def __init__(self, db=None, interval: int = 30) -> None:
        self._db = db
        self._interval = interval
        self._monitor = MonitorAgent()
        settings = get_settings()
        self._executor = ShellExecutor(whitelist=settings.shell_whitelist)
        self._running = False
        self._task: asyncio.Task | None = None
        self._started_at: str | None = None
        self._last_check: str | None = None
        self._check_count = 0

        # Thresholds
        self._thresholds = {
            "cpu": settings.alert_cpu_percent,
            "memory": settings.alert_memory_percent,
            "disk": settings.alert_disk_percent,
            "temperature": settings.alert_temperature_c,
        }

        # Rolling metrics for baseline (last 120 samples = 1 hour at 30s interval)
        self._history: deque[dict] = deque(maxlen=120)

        # Active alerts (keyed by source)
        self._active_alerts: dict[str, Alert] = {}

        # Remediation log
        self._remediation_log: deque[RemediationRecord] = deque(maxlen=200)

        # Cooldown tracker: source → last remediation time
        self._cooldowns: dict[str, float] = {}
        self._cooldown_seconds = 300  # 5 minutes

    # ── Lifecycle ──────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._started_at = datetime.now(timezone.utc).isoformat()
        self._task = asyncio.create_task(self._run_loop())

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
            "started_at": self._started_at,
            "last_check": self._last_check,
            "check_count": self._check_count,
            "active_alerts": len(self._active_alerts),
            "total_remediations": len(self._remediation_log),
            "interval_seconds": self._interval,
            "thresholds": self._thresholds,
        }

    # ── Main Loop ──────────────────────────────────────

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except Exception:
                pass  # Never crash the daemon
            await asyncio.sleep(self._interval)

    async def _tick(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._last_check = now
        self._check_count += 1

        # 1. Collect
        metrics = self._monitor.collect_metrics()
        self._history.append(metrics)
        await self._store_metrics(metrics)

        # 2. Detect
        new_alerts = self._detect(metrics)

        # 3. Auto-resolve old alerts
        self._auto_resolve(metrics)

        # 4. Remediate
        for alert in new_alerts:
            if alert.severity == "critical":
                await self._remediate(alert)

        # 5. Check services
        await self._check_services()

        # 6. Check VPS (every 5th tick = ~150s to avoid SSH overhead)
        if self._check_count % 5 == 0:
            await self._check_vps()

    # ── Collector ──────────────────────────────────────

    async def _store_metrics(self, metrics: dict) -> None:
        if not self._db:
            return
        try:
            await self._db.execute(
                """INSERT INTO metrics_history
                   (timestamp, cpu_usage, memory_usage, disk_usage, temperature, load_avg, network_io)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    metrics.get("timestamp", ""),
                    metrics.get("cpu_percent", 0),
                    metrics.get("memory_percent", 0),
                    metrics.get("disk_percent", 0),
                    metrics.get("temperature", 0),
                    json.dumps(metrics.get("load_avg", [])),
                    json.dumps({
                        "sent_mb": metrics.get("network_sent_mb", 0),
                        "recv_mb": metrics.get("network_recv_mb", 0),
                    }),
                ),
            )
        except Exception:
            pass

    # ── Detector ───────────────────────────────────────

    def _baseline(self, key: str) -> float | None:
        """Calculate rolling average for a metric."""
        values = [m.get(key, 0) for m in self._history if key in m]
        if len(values) < 10:
            return None
        return sum(values) / len(values)

    def _detect(self, metrics: dict) -> list[Alert]:
        now = datetime.now(timezone.utc).isoformat()
        alerts = []

        checks = [
            ("cpu", "cpu_percent", self._thresholds["cpu"]),
            ("memory", "memory_percent", self._thresholds["memory"]),
            ("disk", "disk_percent", self._thresholds["disk"]),
            ("temperature", "temperature", self._thresholds["temperature"]),
        ]

        for source, key, threshold in checks:
            value = metrics.get(key, 0)
            if value is None:
                continue

            severity = None
            if value >= threshold:
                severity = "critical"
            elif value >= threshold * 0.9:
                severity = "warning"
            else:
                # Check baseline anomaly (50% above average)
                baseline = self._baseline(key)
                if baseline and baseline > 0 and value > baseline * 1.5:
                    severity = "warning"

            if severity and source not in self._active_alerts:
                alert = Alert(
                    id=f"{source}-{self._check_count}",
                    severity=severity,
                    source=source,
                    message=f"{source} at {value:.1f}% (threshold: {threshold}%)",
                    value=value,
                    threshold=threshold,
                    timestamp=now,
                )
                self._active_alerts[source] = alert
                alerts.append(alert)
                asyncio.create_task(self._store_alert(alert))

        return alerts

    def _auto_resolve(self, metrics: dict) -> None:
        """Resolve alerts when metrics return to normal."""
        now = datetime.now(timezone.utc).isoformat()
        resolved = []

        for source, alert in self._active_alerts.items():
            key_map = {"cpu": "cpu_percent", "memory": "memory_percent",
                       "disk": "disk_percent", "temperature": "temperature"}
            key = key_map.get(source)
            if not key:
                continue
            value = metrics.get(key, 0)
            threshold = self._thresholds.get(source, 100)

            if value < threshold * 0.85:  # 15% below threshold = resolved
                alert.resolved = True
                alert.resolved_at = now
                resolved.append(source)
                asyncio.create_task(self._resolve_alert_db(alert))

        for source in resolved:
            del self._active_alerts[source]

    async def _store_alert(self, alert: Alert) -> None:
        if not self._db:
            return
        try:
            await self._db.execute(
                "INSERT INTO alerts (timestamp, severity, source, message, resolved) VALUES (?, ?, ?, ?, ?)",
                (alert.timestamp, alert.severity, alert.source, alert.message, False),
            )
        except Exception:
            pass

    async def _resolve_alert_db(self, alert: Alert) -> None:
        if not self._db:
            return
        try:
            await self._db.execute(
                "UPDATE alerts SET resolved = 1, resolved_at = ? WHERE source = ? AND resolved = 0",
                (alert.resolved_at, alert.source),
            )
        except Exception:
            pass

    # ── Remediator ─────────────────────────────────────

    async def _remediate(self, alert: Alert) -> None:
        """Execute remediation playbook for an alert."""
        # Check cooldown
        now = time.monotonic()
        last = self._cooldowns.get(alert.source, 0)
        if now - last < self._cooldown_seconds:
            return

        playbook_key = f"{alert.source}_critical"
        playbook = PLAYBOOKS.get(playbook_key, [])
        if not playbook:
            return

        self._cooldowns[alert.source] = now

        for step in playbook:
            cmd = step["cmd"]
            desc = step["desc"]
            try:
                result = await self._executor.execute(cmd, timeout=30)
                record = RemediationRecord(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    alert_source=alert.source,
                    action=desc,
                    command=cmd,
                    result=result.get("stdout", "")[:500],
                    success=result.get("exit_code", 1) == 0,
                )
            except Exception as e:
                record = RemediationRecord(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    alert_source=alert.source,
                    action=desc,
                    command=cmd,
                    result=str(e)[:500],
                    success=False,
                )
            self._remediation_log.append(record)
            alert.remediation = desc

        # Send webhook event
        await self._send_webhook(alert)

    async def _send_webhook(self, alert: Alert) -> None:
        """Notify via webhook for n8n integration."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(
                    "http://localhost:8420/api/v1/monitor/webhooks/receive",
                    json={
                        "source": "devops-agent",
                        "event": "remediation",
                        "data": {
                            "alert_source": alert.source,
                            "severity": alert.severity,
                            "message": alert.message,
                            "remediation": alert.remediation,
                            "timestamp": alert.timestamp,
                        },
                    },
                )
        except Exception:
            pass

    # ── Service Checker ────────────────────────────────

    async def _check_services(self) -> None:
        """Check critical systemd services and Docker containers."""
        now = datetime.now(timezone.utc).isoformat()

        # Systemd services
        for svc in CRITICAL_SERVICES:
            try:
                result = await self._executor.execute(f"systemctl is-active {svc}", timeout=5)
                if result.get("stdout", "").strip() != "active":
                    source = f"service:{svc}"
                    if source not in self._active_alerts:
                        alert = Alert(
                            id=f"{source}-{self._check_count}",
                            severity="critical",
                            source=source,
                            message=f"Service {svc} is not active",
                            value=0, threshold=1, timestamp=now,
                        )
                        self._active_alerts[source] = alert
                        await self._remediate_service(svc, alert)
            except Exception:
                pass

        # Docker containers
        for container in CRITICAL_CONTAINERS:
            try:
                result = await self._executor.execute(
                    f"docker ps --filter name={container} --format '{{{{.Status}}}}'", timeout=5
                )
                status = result.get("stdout", "").strip()
                if not status or "Up" not in status:
                    source = f"docker:{container}"
                    if source not in self._active_alerts:
                        alert = Alert(
                            id=f"{source}-{self._check_count}",
                            severity="critical",
                            source=source,
                            message=f"Container {container} is not running",
                            value=0, threshold=1, timestamp=now,
                        )
                        self._active_alerts[source] = alert
                        await self._remediate_container(container, alert)
            except Exception:
                pass

    async def _check_vps(self) -> None:
        """Check production VPS containers via SSH bridge."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            result = await self._executor.execute(
                f"{VPS_SSH} 'docker ps --format \"{{{{.Names}}}}:{{{{.Status}}}}\"'", timeout=10
            )
            if result.get("exit_code", 1) != 0:
                source = "vps:offline"
                if source not in self._active_alerts:
                    self._active_alerts[source] = Alert(
                        id=f"{source}-{self._check_count}", severity="critical",
                        source=source, message="VPS is unreachable",
                        value=0, threshold=1, timestamp=now,
                    )
                return

            # Auto-resolve VPS offline alert
            if "vps:offline" in self._active_alerts:
                self._active_alerts["vps:offline"].resolved = True
                del self._active_alerts["vps:offline"]

            # Check each critical container
            running = result.get("stdout", "")
            for container in VPS_CONTAINERS:
                source = f"vps:{container}"
                if container not in running or "Up" not in running.split(container)[1].split("\n")[0] if container in running else True:
                    # Container might be down — simple check
                    if f"{container}:" not in running:
                        if source not in self._active_alerts:
                            self._active_alerts[source] = Alert(
                                id=f"{source}-{self._check_count}", severity="warning",
                                source=source, message=f"VPS container {container} not running",
                                value=0, threshold=1, timestamp=now,
                            )
                else:
                    # Running — auto-resolve if was alerting
                    if source in self._active_alerts:
                        self._active_alerts[source].resolved = True
                        del self._active_alerts[source]
        except Exception:
            pass

    async def _remediate_service(self, service: str, alert: Alert) -> None:
        now = time.monotonic()
        source = f"service:{service}"
        if now - self._cooldowns.get(source, 0) < self._cooldown_seconds:
            return
        self._cooldowns[source] = now

        cmd = f"systemctl restart {service}"
        try:
            result = await self._executor.execute(cmd, timeout=15)
            record = RemediationRecord(
                timestamp=datetime.now(timezone.utc).isoformat(),
                alert_source=source, action=f"Restart {service}",
                command=cmd, result=result.get("stdout", "")[:200],
                success=result.get("exit_code", 1) == 0,
            )
        except Exception as e:
            record = RemediationRecord(
                timestamp=datetime.now(timezone.utc).isoformat(),
                alert_source=source, action=f"Restart {service}",
                command=cmd, result=str(e)[:200], success=False,
            )
        self._remediation_log.append(record)
        alert.remediation = f"Restart {service}"
        await self._send_webhook(alert)

    async def _remediate_container(self, container: str, alert: Alert) -> None:
        now = time.monotonic()
        source = f"docker:{container}"
        if now - self._cooldowns.get(source, 0) < self._cooldown_seconds:
            return
        self._cooldowns[source] = now

        cmd = f"docker start {container}"
        try:
            result = await self._executor.execute(cmd, timeout=15)
            record = RemediationRecord(
                timestamp=datetime.now(timezone.utc).isoformat(),
                alert_source=source, action=f"Start {container}",
                command=cmd, result=result.get("stdout", "")[:200],
                success=result.get("exit_code", 1) == 0,
            )
        except Exception as e:
            record = RemediationRecord(
                timestamp=datetime.now(timezone.utc).isoformat(),
                alert_source=source, action=f"Start {container}",
                command=cmd, result=str(e)[:200], success=False,
            )
        self._remediation_log.append(record)
        alert.remediation = f"Start {container}"
        await self._send_webhook(alert)

    # ── Query Methods (for API) ────────────────────────

    @property
    def active_alerts(self) -> list[dict]:
        return [
            {
                "id": a.id, "severity": a.severity, "source": a.source,
                "message": a.message, "value": a.value, "threshold": a.threshold,
                "timestamp": a.timestamp, "remediation": a.remediation,
            }
            for a in self._active_alerts.values()
        ]

    @property
    def remediation_history(self) -> list[dict]:
        return [
            {
                "timestamp": r.timestamp, "alert_source": r.alert_source,
                "action": r.action, "command": r.command,
                "result": r.result, "success": r.success,
            }
            for r in reversed(self._remediation_log)
        ]

    @property
    def playbooks(self) -> dict:
        return {k: [s["desc"] for s in v] for k, v in PLAYBOOKS.items()}

    @property
    def metrics_buffer(self) -> list[dict]:
        return list(self._history)

    async def get_alerts_history(self, limit: int = 50, severity: str | None = None) -> list[dict]:
        if not self._db:
            return []
        query = "SELECT * FROM alerts ORDER BY timestamp DESC LIMIT ?"
        params: tuple = (limit,)
        if severity:
            query = "SELECT * FROM alerts WHERE severity = ? ORDER BY timestamp DESC LIMIT ?"
            params = (severity, limit)
        rows = await self._db.fetch_all(query, params)
        return [dict(r) for r in rows]

    async def get_metrics_history(self, minutes: int = 30) -> list[dict]:
        if not self._db:
            return list(self._history)
        rows = await self._db.fetch_all(
            """SELECT * FROM metrics_history
               WHERE timestamp > datetime('now', ?)
               ORDER BY timestamp DESC LIMIT 500""",
            (f"-{minutes} minutes",),
        )
        return [dict(r) for r in rows]
