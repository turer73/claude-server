"""Autonomous DevOps Agent — monitors, detects anomalies, auto-remediates.

Facade: lifecycle + orchestration live here; behavior is composed from mixins in
the app.core.devops package. Model/constant re-exports below keep the historical
import paths (e.g. `from app.core.devops_agent import PLAYBOOKS`) working."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime

from app.core.config import get_settings, read_env_var
from app.core.devops.diagnosis import DiagnosisMixin
from app.core.devops.escalation import EscalationMixin
from app.core.devops.metrics import MetricsMixin
from app.core.devops.models import (
    _GOVERNOR_RE,
    _SUSTAINED_N,
    _SUSTAINED_SOURCES,
    _VALID_GOVERNOR,
    _VALID_UNIT,
    PLAYBOOKS,
    VPS_HOST,
    VPS_PROBE_B64,
    VPS_PROBE_SCRIPT,
    VPS_SSH,
    Alert,
    RemediationRecord,
    parse_vps_probe,
)
from app.core.devops.probe import ProbeMixin
from app.core.devops.remediation import RemediationMixin
from app.core.monitor_agent import MonitorAgent
from app.core.shell_executor import ShellExecutor

__all__ = [
    "DevOpsAgent",
    "PLAYBOOKS",
    "VPS_HOST",
    "VPS_PROBE_B64",
    "VPS_PROBE_SCRIPT",
    "VPS_SSH",
    "Alert",
    "RemediationRecord",
    "parse_vps_probe",
    "_GOVERNOR_RE",
    "_SUSTAINED_N",
    "_SUSTAINED_SOURCES",
    "_VALID_GOVERNOR",
    "_VALID_UNIT",
]


class DevOpsAgent(
    MetricsMixin,
    RemediationMixin,
    DiagnosisMixin,
    EscalationMixin,
    ProbeMixin,
):
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

        # INTERV: auto-rollback durumu. source → {"kind","state","command"} (aksiyon-öncesi
        # yakalanan geri-alma bilgisi). _last_rollback: anti-flapping (aynı kaynağı kısa sürede
        # tekrar-tekrar geri-alma; cooldown içinde rollback ATLA, doğrudan escalate).
        self._rollback_state: dict[str, dict] = {}
        self._last_rollback: dict[str, float] = {}
        self._rollback_cooldown = 600  # 10 dk: bu süre içinde aynı kaynak için 2. rollback yok

        # Persistent-critical re-eskalasyon: kalıcı critical alert dedup nedeniyle bir
        # kez bildirilir; çözülene dek her _escalation_interval'da yeniden ping (okunmamış/
        # ele-alınmamış critical sessizce unutulmasın). cron-fail'ler zaten her run'da
        # yeni-event basar → doğal re-ping; bu yalnız devops-metrik/servis alert'leri için.
        self._last_escalation: dict[str, float] = {}
        self._escalation_interval = 1800  # 30 dk

        # ── Read-only teşhis asistanı (vizyon B-bulgusu: playbook semptom-bastırır, kök-neden
        # teşhisi yok). Sustained-critical alert'te memory'deki son değişiklikleri + alert'i
        # Ollama'ya verip MUHTEMEL kök-neden hipotezi üretir, diagnosis event'i emit eder
        # (notify-cron Telegram'a çevirir). KOMUT ÇALIŞTIRMAZ — yalnız okur+öneri. Fail-silent;
        # alert akışını asla bozmaz. once/incident (auto-resolve'da sıfırlanır → tekrarda yeniden).
        self._diagnostic_enabled = (read_env_var("DEVOPS_DIAGNOSTIC_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")
        # Teşhis modeli = GERÇEK route (LLM_ROUTE_DIAGNOSIS override'ı; default tablo qwen2.5:3b).
        # Display + label için; üretimde generate(task="diagnosis") route'u kendi seçer (explicit GEÇME).
        from app.core.agents.llmcore import llm_core as _lc

        self._diag_model = _lc.route("diagnosis")[1]
        self._diag_timeout = 25
        self._diag_memory_db = "/opt/linux-ai-server/data/claude_memory.db"
        self._diagnosed: set[str] = set()

        # VPS-probe sustained-gate: tek geçici SSH-blip'i (probe None) ANINDA vps:offline'a
        # çevirme — N ardışık-fail gerek (metrik _sustained_high felsefesi). WAN-fix yalnız
        # local-internet-down'ı ayırıyordu; geçici-VPS-probe-blip (local up) yine false
        # vps:offline üretiyordu (2026-06-19 restart-yoğunluğu + WAN-blip kaskadı).
        self._vps_probe_fails = 0
        self._vps_fail_threshold = int(read_env_var("VPS_FAIL_THRESHOLD") or "2")

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

        # 3b. Persistent-critical re-eskalasyon (çözülmeyen critical'i pingle)
        await self._escalate_persistent()

        # 4. Remediate (+ read-only teşhis hipotezi — komut çalıştırmaz, fail-silent)
        for alert in new_alerts:
            if alert.severity == "critical":
                await self._remediate(alert)
                self._maybe_diagnose(alert)

        # 5. Check services
        await self._check_services()

        # 6. Check VPS (every 5th tick = ~150s to avoid SSH overhead)
        if self._check_count % 5 == 0:
            await self._check_vps()
