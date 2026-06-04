"""Autonomous DevOps Agent — monitors, detects anomalies, auto-remediates."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.events import emit_event
from app.core.monitor_agent import MonitorAgent
from app.core.shell_executor import ShellExecutor


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
        {
            "desc": "Set CPU governor to powersave",
            "cmd": "cpufreq-set -g powersave 2>/dev/null || echo 'powersave' | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null || true",
        },
    ],
    "service_down": [
        {"desc": "Restart service", "cmd": "systemctl restart {service}"},
    ],
    "docker_down": [
        {"desc": "Start container", "cmd": "docker start {container}"},
    ],
}

VPS_HOST = os.environ.get("VPS_HOST", "")
VPS_SSH = f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 {VPS_HOST}"

# Fixed, internal probe run on the VPS to collect host metrics + container state.
# Single sample of /proc/stat deltas (no tool dependency beyond awk/free/df/docker).
VPS_PROBE_SCRIPT = """\
read -r _ a1 b1 c1 d1 _ < /proc/stat
sleep 1
read -r _ a2 b2 c2 d2 _ < /proc/stat
awk -v db=$(( (a2+b2+c2)-(a1+b1+c1) )) -v dt=$(( (a2+b2+c2+d2)-(a1+b1+c1+d1) )) 'BEGIN{printf "CPU=%.1f\\n",(dt>0)?db*100/dt:0}'
free | awk '/^Mem:/{printf "MEM=%.1f\\n",$3/$2*100}'
df / | awk 'NR==2{gsub(/%/,"",$5);print "DISK="$5}'
echo "CTOTAL=$(docker ps -aq 2>/dev/null | wc -l)"
echo "CUP=$(docker ps -q 2>/dev/null | wc -l)"
echo "NAMES=$(docker ps --format '{{.Names}}' 2>/dev/null | tr '\\n' ',')"
"""
# base64 so it travels as a single quote-safe token through `ssh host '...'`
VPS_PROBE_B64 = base64.b64encode(VPS_PROBE_SCRIPT.encode()).decode()


def parse_vps_probe(stdout: str) -> dict:
    """Parse KEY=VALUE lines emitted by VPS_PROBE_SCRIPT into a typed dict."""
    kv: dict[str, str] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()

    def _f(key: str) -> float | None:
        try:
            return float(kv[key])
        except (KeyError, ValueError):
            return None

    def _i(key: str) -> int | None:
        try:
            return int(kv[key])
        except (KeyError, ValueError):
            return None

    return {
        "cpu": _f("CPU"),
        "mem": _f("MEM"),
        "disk": _f("DISK"),
        "containers_total": _i("CTOTAL"),
        "containers_up": _i("CUP"),
        "names": [n for n in kv.get("NAMES", "").split(",") if n],
    }


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

        # Watchlists (settings-driven; defaults match current host state)
        self._critical_services = list(settings.monitor_critical_services)
        self._critical_containers = list(settings.monitor_critical_containers)
        self._vps_containers = list(settings.monitor_vps_containers)
        self._vps_host = settings.vps_host

        # Latest VPS sample (for status/dashboard without a DB round-trip)
        self._latest_vps: dict = {}

        # Rolling metrics for baseline (last 120 samples = 1 hour at 30s interval)
        self._history: deque[dict] = deque(maxlen=120)

        # Active alerts (keyed by source)
        self._active_alerts: dict[str, Alert] = {}

        # LIVESYS Faz 5 — otonom remediation kapısı (notify=güvenli-default, exec YOK).
        self._remediation_mode = settings.remediation_mode
        # S2: verify öncesi grace (restart/cleanup etkisi otursun). Test'te 0'lanır.
        self._verify_grace = 2

        # Remediation log (in-memory hızlı-erişim; kalıcı kayıt -> remediation_log tablosu)
        self._remediation_log: deque[RemediationRecord] = deque(maxlen=200)

        # Cooldown tracker: source → last remediation time
        self._cooldowns: dict[str, float] = {}
        self._cooldown_seconds = 300  # 5 minutes

    # ── Lifecycle ──────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._started_at = datetime.now(UTC).isoformat()
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
        now = datetime.now(UTC).isoformat()
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
                    json.dumps(
                        {
                            "sent_mb": metrics.get("network_sent_mb", 0),
                            "recv_mb": metrics.get("network_recv_mb", 0),
                        }
                    ),
                ),
            )
        except Exception:
            pass

    # ── Detector ───────────────────────────────────────

    def _detect(self, metrics: dict) -> list[Alert]:
        now = datetime.now(UTC).isoformat()
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
        now = datetime.now(UTC).isoformat()
        resolved = []

        for source, alert in self._active_alerts.items():
            key_map = {"cpu": "cpu_percent", "memory": "memory_percent", "disk": "disk_percent", "temperature": "temperature"}
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
        # LIVESYS Faz 3.2 alerts-bridge: aynı threshold-alert'i merkezi events'e de
        # yaz (TEK-writer noktası, scatter yok). alerts-INSERT KALIR (active_alerts/
        # retention bağımlı). severity "warning"->warn _normalize_severity ile.
        # KAYIT-ONLY: bildirim AYRI notify-cron'un işi (henüz yok); alerts bugüne dek
        # zaten push-edilmiyordu -> double-notify yok. emit_event sync (sqlite3) ->
        # event-loop'u bloklamamak için to_thread; best-effort, devops_agent'ı bozmaz.
        try:
            await asyncio.to_thread(
                emit_event,
                type="alert",
                source=alert.source,
                title=alert.message,
                severity=alert.severity,
                detail=None,
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
        # Check cooldown — only blocks if we've actually remediated this source
        # before. The previous default of 0 broke on freshly-booted hosts where
        # time.monotonic() < cooldown_seconds.
        now = time.monotonic()
        last = self._cooldowns.get(alert.source)
        if last is not None and (now - last) < self._cooldown_seconds:
            return

        playbook_key = f"{alert.source}_critical"
        playbook = PLAYBOOKS.get(playbook_key, [])
        if not playbook:
            return

        self._cooldowns[alert.source] = now

        for step in playbook:
            await self._apply_remediation(alert, alert.source, step["desc"], step["cmd"])

        # Send webhook event (n8n) — mode dahil
        await self._send_webhook(alert)
        # FAZ5-S2: verify -> fail ise escalate (yalnız mode=auto)
        await self._verify_and_escalate(alert.source, alert)

    async def _apply_remediation(self, alert: Alert, source: str, action: str, command: str, timeout: int = 30) -> None:
        """Tek-nokta remediation adımı — TÜM yollar (playbook + servis + container)
        bunu kullanır (Codex P1: gate her yerde). mode-gate: 'auto' değilse YÜRÜTME
        YOK, sadece niyet kaydedilir (mevcut alert-notify escalate eder). in-memory
        log + kalıcı ledger + alert.remediation."""
        mode = self._remediation_mode
        executed = False
        success: bool | None = None
        if mode == "auto":
            # OPT-IN: gerçekten yürüt (eski davranış). Yıkıcı adımlar mümkün
            # (prune --volumes / rm backup / restart). FAZ5-S2: aksiyon sonrası
            # _verify_and_escalate verify eder (fail -> escalate). Çoğu yıkıcı-aksiyon
            # geri-alınamaz -> rollback YALNIZ reversible (governor); gerisi escalate.
            executed = True
            try:
                result = await self._executor.execute(command, timeout=timeout)
                out = result.get("stdout", "")[:500]
                success = result.get("exit_code", 1) == 0
            except Exception as e:
                out = str(e)[:500]
                success = False
        else:
            out = f"skipped: remediation_mode={mode} (otonom yürütme kapalı)"
        self._remediation_log.append(
            RemediationRecord(
                timestamp=datetime.now(UTC).isoformat(),
                alert_source=source,
                action=action,
                command=command,
                result=out,
                success=bool(success),
            )
        )
        # executed -> verify_status NULL (S2 verify-edecek); değilse 'skipped'
        # (notify/dry_run satırları verify-UPDATE'inden ayrışsın).
        await self._persist_remediation_row(
            source,
            alert.severity,
            mode,
            action,
            command,
            executed,
            out,
            success,
            verify_status=None if executed else "skipped",
        )
        alert.remediation = f"[{mode}] {action}"

    async def _persist_remediation_row(
        self,
        source: str,
        severity: str,
        mode: str,
        action: str,
        command: str,
        executed: bool,
        result: str,
        success: bool | None,
        verify_status: str | None = None,
    ) -> None:
        """Kalıcı remediation ledger (server.db.remediation_log). Best-effort:
        DB yoksa/yazamazsa sessizce geç (remediation akışını bozma)."""
        if not self._db:
            return
        try:
            await self._db.execute(
                "INSERT INTO remediation_log "
                "(alert_source, severity, mode, action, command, executed, result, success, verify_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source,
                    severity,
                    mode,
                    action,
                    command,
                    1 if executed else 0,
                    result,
                    None if success is None else (1 if success else 0),
                    verify_status,
                ),
            )
        except Exception:
            pass

    # ── LIVESYS Faz 5 Slice-2: verify -> escalate ──────────────

    async def _verify_remediation(self, source: str) -> bool | None:
        """Aksiyon sonrası health re-check. True=düzeldi, False=hâlâ sorunlu,
        None=verify-edilemez (cpu sadece-log / belirsiz). Heuristik: cleanup etkisi
        gecikebilir -> False-fail mümkün (sonucu sadece escalate-notify, yıkıcı değil)."""
        base = source.split(":", 1)[0]
        try:
            if base == "service":
                svc = source.split(":", 1)[1]
                r = await self._executor.execute(f"systemctl is-active {svc}", timeout=10)
                return r.get("stdout", "").strip() == "active"
            if base == "docker":
                cont = source.split(":", 1)[1]
                r = await self._executor.execute(f"docker inspect -f '{{{{.State.Running}}}}' {cont}", timeout=10)
                return "true" in r.get("stdout", "").lower()
            # metrik playbook: yeniden örnekle. cpu_critical SADECE log -> verify yok.
            key = {"memory": "memory_percent", "disk": "disk_percent", "temperature": "temperature"}.get(base)
            if not key:
                return None
            metrics = self._monitor.collect_metrics()
            val = metrics.get(key)
            thr = self._thresholds.get(base)
            if val is None or thr is None:
                return None
            return val < thr
        except Exception:
            return None  # verify-edilemedi -> belirsiz (escalate etme)

    async def _verify_and_escalate(self, source: str, alert: Alert) -> None:
        """FAZ5-S2: yalnız mode=auto (notify'da exec yok -> verify-edecek şey yok).
        verify -> ledger.verify_status güncelle; fail -> escalate (critical event +
        escalated=1). Rollback: çoğu aksiyon geri-alınamaz -> escalate (manuel müdahale)."""
        if self._remediation_mode != "auto":
            return
        # kısa grace: restart/cleanup etkisinin oturması için (False-fail azalt).
        if self._verify_grace:
            await asyncio.sleep(self._verify_grace)
        ok = await self._verify_remediation(source)
        status = "n/a" if ok is None else ("pass" if ok else "fail")
        escalated = status == "fail"
        if self._db:
            try:
                await self._db.execute(
                    "UPDATE remediation_log SET verify_status=?, escalated=? WHERE alert_source=? AND verify_status IS NULL",
                    (status, 1 if escalated else 0, source),
                )
            except Exception:
                pass
        if escalated:
            # ESCALATE: otonom remediation çalıştı ama sorun sürüyor -> manuel müdahale.
            try:
                await asyncio.to_thread(
                    emit_event,
                    type="alert",
                    source=f"remediation:{source}",
                    title=f"Otonom remediation BAŞARISIZ: {source} hâlâ kritik — manuel müdahale gerek",
                    severity="critical",
                    detail=f"auto-remediation yürütüldü ama verify başarısız ({alert.message}).",
                )
            except Exception:
                pass

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
        now = datetime.now(UTC).isoformat()

        # Systemd services
        for svc in self._critical_services:
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
                            value=0,
                            threshold=1,
                            timestamp=now,
                        )
                        self._active_alerts[source] = alert
                        await self._remediate_service(svc, alert)
            except Exception:
                pass

        # Docker containers
        for container in self._critical_containers:
            try:
                result = await self._executor.execute(f"docker ps --filter name={container} --format '{{{{.Status}}}}'", timeout=5)
                status = result.get("stdout", "").strip()
                if not status or "Up" not in status:
                    source = f"docker:{container}"
                    if source not in self._active_alerts:
                        alert = Alert(
                            id=f"{source}-{self._check_count}",
                            severity="critical",
                            source=source,
                            message=f"Container {container} is not running",
                            value=0,
                            threshold=1,
                            timestamp=now,
                        )
                        self._active_alerts[source] = alert
                        await self._remediate_container(container, alert)
            except Exception:
                pass

    async def _vps_ssh_probe(self) -> dict | None:
        """Run the fixed VPS metric probe over SSH via an isolated subprocess.

        Bypasses the user-facing ShellExecutor on purpose: this is a fixed,
        internal command with no user input, and `ssh` is deliberately absent
        from shell_whitelist (routing it through the executor raises
        AuthorizationError). Returns the parsed sample, or None if the VPS is
        unreachable / the output is unusable.
        """
        if not self._vps_host:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "BatchMode=yes",
                self._vps_host,
                f"echo {VPS_PROBE_B64} | base64 -d | bash",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        except (TimeoutError, OSError):
            return None
        if proc.returncode != 0:
            return None
        parsed = parse_vps_probe(out.decode(errors="replace"))
        if parsed["cpu"] is None:  # partial/unparseable output → treat as failure
            return None
        return parsed

    async def _store_vps_metrics(self, sample: dict, online: bool) -> None:
        if not self._db:
            return
        try:
            await self._db.execute(
                """INSERT INTO vps_metrics_history
                   (timestamp, online, cpu_usage, memory_usage, disk_usage, containers_total, containers_up)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now(UTC).isoformat(),
                    1 if online else 0,
                    sample.get("cpu"),
                    sample.get("mem"),
                    sample.get("disk"),
                    sample.get("containers_total"),
                    sample.get("containers_up"),
                ),
            )
        except Exception:
            pass

    async def _check_vps(self) -> None:
        """Collect VPS host metrics + container state via the SSH probe, persist, alert."""
        now = datetime.now(UTC).isoformat()
        probe = await self._vps_ssh_probe()

        if probe is None:
            await self._store_vps_metrics({}, online=False)
            self._latest_vps = {"online": False, "timestamp": now}
            source = "vps:offline"
            if source not in self._active_alerts:
                self._active_alerts[source] = Alert(
                    id=f"{source}-{self._check_count}",
                    severity="critical",
                    source=source,
                    message="VPS is unreachable",
                    value=0,
                    threshold=1,
                    timestamp=now,
                )
            return

        await self._store_vps_metrics(probe, online=True)
        self._latest_vps = {**probe, "online": True, "timestamp": now}

        # Auto-resolve VPS offline alert
        if "vps:offline" in self._active_alerts:
            del self._active_alerts["vps:offline"]

        # Per-container down/up alerts (exact name match against running set)
        running = set(probe.get("names", []))
        for container in self._vps_containers:
            source = f"vps:{container}"
            if container not in running:
                if source not in self._active_alerts:
                    self._active_alerts[source] = Alert(
                        id=f"{source}-{self._check_count}",
                        severity="warning",
                        source=source,
                        message=f"VPS container {container} not running",
                        value=0,
                        threshold=1,
                        timestamp=now,
                    )
            elif source in self._active_alerts:
                del self._active_alerts[source]

    async def _remediate_service(self, service: str, alert: Alert) -> None:
        now = time.monotonic()
        source = f"service:{service}"
        # taze-boot bug fix (Codex-CI): get(source,0)+monotonic<cooldown erken-return
        # yapardi -> None-check (devops _remediate ile ayni).
        last = self._cooldowns.get(source)
        if last is not None and (now - last) < self._cooldown_seconds:
            return
        self._cooldowns[source] = now

        # mode-gate (Codex P1): notify/dry_run'da systemctl restart YÜRÜTÜLMEZ.
        await self._apply_remediation(alert, source, f"Restart {service}", f"systemctl restart {service}", timeout=15)
        await self._send_webhook(alert)
        await self._verify_and_escalate(source, alert)

    async def _remediate_container(self, container: str, alert: Alert) -> None:
        now = time.monotonic()
        source = f"docker:{container}"
        # taze-boot bug fix (Codex-CI): get(source,0)+monotonic<cooldown erken-return
        # yapardi -> None-check (devops _remediate ile ayni).
        last = self._cooldowns.get(source)
        if last is not None and (now - last) < self._cooldown_seconds:
            return
        self._cooldowns[source] = now

        # mode-gate (Codex P1): notify/dry_run'da docker start YÜRÜTÜLMEZ.
        await self._apply_remediation(alert, source, f"Start {container}", f"docker start {container}", timeout=15)
        await self._send_webhook(alert)
        await self._verify_and_escalate(source, alert)

    # ── Query Methods (for API) ────────────────────────

    @property
    def active_alerts(self) -> list[dict]:
        return [
            {
                "id": a.id,
                "severity": a.severity,
                "source": a.source,
                "message": a.message,
                "value": a.value,
                "threshold": a.threshold,
                "timestamp": a.timestamp,
                "remediation": a.remediation,
            }
            for a in self._active_alerts.values()
        ]

    @property
    def remediation_history(self) -> list[dict]:
        return [
            {
                "timestamp": r.timestamp,
                "alert_source": r.alert_source,
                "action": r.action,
                "command": r.command,
                "result": r.result,
                "success": r.success,
            }
            for r in reversed(self._remediation_log)
        ]

    @property
    def playbooks(self) -> dict:
        return {k: [s["desc"] for s in v] for k, v in PLAYBOOKS.items()}

    @property
    def metrics_buffer(self) -> list[dict]:
        return list(self._history)

    @property
    def latest_vps(self) -> dict:
        return self._latest_vps

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

    async def get_vps_metrics_history(self, minutes: int = 60) -> list[dict]:
        if not self._db:
            return []
        rows = await self._db.fetch_all(
            """SELECT * FROM vps_metrics_history
               WHERE timestamp > datetime('now', ?)
               ORDER BY timestamp DESC LIMIT 500""",
            (f"-{minutes} minutes",),
        )
        return [dict(r) for r in rows]
