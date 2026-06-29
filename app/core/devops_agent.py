"""Autonomous DevOps Agent — monitors, detects anomalies, auto-remediates."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shlex
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.config import get_settings, read_env_var
from app.core.events import emit_event
from app.core.monitor_agent import MonitorAgent
from app.core.provenance import provenance_json
from app.core.shell_executor import ShellExecutor

# INTERV: yalnız GERİ-ALINABİLİR komutlar rollback'e uygun (DAR set). cpu-governor değişimi
# tersine çevrilebilir (önceki governor'a dön); prune/delete/truncate/restart GERİ-ALINAMAZ
# (escalate-only — surer kuralı). Governor adı güvenlik için katı doğrulanır.
_GOVERNOR_RE = re.compile(r"scaling_governor|cpufreq-set\s+-g")
_VALID_GOVERNOR = re.compile(r"^[a-z]+$")  # ondemand/schedutil/performance/powersave...

# GÜVENLİK: otonom remediation'da servis/konteyner adı f-string ile TAM-SHELL
# komutuna giriyor (ShellExecutor=create_subprocess_shell; whitelist yalnız ilk
# komuta bakar, zincir geçer). Adlar config'ten gelir (monitor_critical_*) ama
# config-drift geçmişi var (canlı server.yml repo'yu ezer) → savunma-derinliği:
# katı ad-doğrulama + shlex.quote. systemd unit + docker name güvenli karakter
# kümesi (@ . _ : - izinli; boşluk/meta YASAK).
_VALID_UNIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:-]*$")

# #567 FP fix: sustained-window gating. cpu/mem/disk geçici-zirve eğilimli (zamanlanmış
# ağır iş — test-runner/e2e gece bakım penceresinde %98 anlık zirve yapıyor ama sürekli
# değil). Critical SADECE son N örnek de eşik-üstüyse (sürdürülen-yük). temperature hariç
# (fiziksel — tek yüksek okuma gerçek, thermal yanıt anlık olmalı).
_SUSTAINED_N = int(os.environ.get("ALERT_SUSTAINED_SAMPLES", "3"))  # 3×30s = 90s sürdürülen
_SUSTAINED_SOURCES = frozenset({"cpu", "memory", "disk"})


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
    # GUVENLIK (FAZ5 playbook-safening): YIKICI/geri-alinamaz adimlar cikarildi.
    # `--volumes` (adli/named volume veri-silme) ve otonom-backup-silme YASAK —
    # auto-mode'da false-positive critical'de veri kaybi riski. Sadece guvenli
    # reclaim: image/container/network prune (volume HARIC), cache/eski-tmp temizle,
    # dev log truncate. Backup-rotation = daily-backup.sh'in isi, remediation'in DEGIL.
    "memory_critical": [
        {"desc": "Docker prune (volume HARIC)", "cmd": "docker system prune -f 2>/dev/null || true"},
        {"desc": "Clear pip cache", "cmd": "pip cache purge 2>/dev/null || true"},
        {"desc": "Clear old tmp files", "cmd": "find /tmp -type f -mtime +1 -delete 2>/dev/null || true"},
    ],
    "disk_critical": [
        {"desc": "Docker prune (volume HARIC)", "cmd": "docker system prune -f 2>/dev/null || true"},
        {"desc": "Truncate dev logs", "cmd": "find /var/log -name '*.log' -size +50M -exec truncate -s 10M {} \\; 2>/dev/null || true"},
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
        {"desc": "Restart container", "cmd": "docker restart {container}"},
    ],
}

VPS_HOST = os.environ.get("VPS_HOST", "")
VPS_SSH = f"ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 {VPS_HOST}"

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

    def _is_in_backup_window(self) -> bool:
        """03:00-05:00 UTC: daily-backup (03:00) + restore-test (03:20) + pull-vps-backup (04:20).
        Bu pencerede CPU yükselmesi meşru — klipper #100224 FP-fix.
        Override: BACKUP_GRACE_START_HOUR / BACKUP_GRACE_END_HOUR env-var (UTC saat, int)."""
        try:
            start = int(read_env_var("BACKUP_GRACE_START_HOUR") or "3")
            end = int(read_env_var("BACKUP_GRACE_END_HOUR") or "5")
        except (ValueError, TypeError):
            return False
        return start <= datetime.now(UTC).hour < end

    def _sustained_high(self, key: str, threshold: float) -> bool:
        """Son _SUSTAINED_N örnek (current dahil — _history'ye _detect'ten ÖNCE append edilir)
        eşik-üstü mü → sürdürülen-yük. Geçici zirveyi (zamanlanmış toplu-iş) filtreler.
        Yeterli geçmiş yoksa (<N örnek, startup) False — sürdürülen doğrulanamaz, critical etme."""
        recent = list(self._history)[-_SUSTAINED_N:]
        vals = [m.get(key) for m in recent if m.get(key) is not None]
        if len(vals) < _SUSTAINED_N:
            return False
        return all(v >= threshold for v in vals)

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

            # klipper #100224: backup-window CPU grace (03:00-05:00 UTC). daily-backup /
            # restore-test / pull-vps-backup bu pencerede anlık CPU %95+ yapıyor — meşru,
            # FP önleme. Diğer metrikler (disk/mem/temp) bu pencerede yine izlenir.
            if source == "cpu" and self._is_in_backup_window():
                continue

            severity = None
            if value >= threshold:
                # Sustained-window gating (#567): cpu/mem/disk geçici-zirvede critical
                # üretmesin — son N örnek de eşik-üstü olmalı. Eşik-üstü ama sürdürülmemiş
                # → warning (soft; remediate/escalate/critical-Telegram YOK). temperature
                # ve diğerleri tek-örnek critical (fiziksel/anlık).
                unsustained = source in _SUSTAINED_SOURCES and not self._sustained_high(key, threshold)
                severity = "warning" if unsustained else "critical"
            elif value >= threshold * 0.9:
                severity = "warning"

            # Codex P1: yeni-alert VEYA warning→critical YÜKSELTME. sustained-gating
            # sonrası ilk-örnek warning olarak aktif-slotu tutar; sürdürülen olunca
            # 'source not in _active_alerts' guard'ı critical'i engellerdi → gerçek
            # sürekli-yük asla escalate olmazdı. Upgrade ile çözülür.
            existing = self._active_alerts.get(source)
            is_upgrade = existing is not None and existing.severity == "warning" and severity == "critical"
            if severity and (existing is None or is_upgrade):
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
                # re-eskalasyon saati _escalate_persistent'te ilk-görülmede başlatılır
                # (tek-nokta, tüm kaynaklar için uniform).
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
            self._last_escalation.pop(source, None)  # çözüldü -> eskalasyon-saati sıfırla
            self._diagnosed.discard(source)  # çözüldü -> tekrarında yeniden teşhis et

    async def _store_alert(self, alert: Alert) -> None:
        if not self._db:
            return
        try:
            await self._db.execute(
                "INSERT INTO alerts (timestamp, severity, source, message, resolved, valid_at) VALUES (?, ?, ?, ?, ?, ?)",
                (alert.timestamp, alert.severity, alert.source, alert.message, False, alert.timestamp),
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
                "UPDATE alerts SET resolved = 1, resolved_at = ?, invalid_at = ? WHERE source = ? AND resolved = 0",
                (alert.resolved_at, alert.resolved_at, alert.source),
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
            # OPT-IN: gerçekten yürüt. Playbook'lar güvenlileştirildi (yıkıcı/geri-
            # alınamaz adımlar — prune --volumes / backup-silme — çıkarıldı); kalanlar
            # güvenli-reclaim + restart. FAZ5-S2: aksiyon sonrası _verify_and_escalate
            # verify eder (fail -> escalate). INTERV: reversible-komutta aksiyon-ÖNCESİ
            # geri-alma durumu yakalanır (verify-fail -> auto-rollback).
            await self._capture_rollback(source, command)
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
        # (notify/dry_run satırları verify-UPDATE'inden ayrışsın). INTERV: her satıra
        # provenance (tetik-kökeni) iliştir — sonradan denetlenebilir.
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
            provenance=provenance_json(alert, mode, detected_at=getattr(alert, "timestamp", None) or None),
        )
        alert.remediation = f"[{mode}] {action}"

    # ── INTERV: reversible-set + auto-rollback ─────────────────

    def _reversible_kind(self, command: str) -> str | None:
        """Komut GERİ-ALINABİLİR mi (DAR set). Şimdilik yalnız cpu-governor değişimi.
        prune/delete/truncate/restart -> None (geri-alınamaz, escalate-only)."""
        if _GOVERNOR_RE.search(command):
            return "governor"
        return None

    async def _capture_rollback(self, source: str, command: str) -> None:
        """Aksiyon-ÖNCESİ geri-alma durumunu yakala (yalnız reversible komut). governor:
        mevcut scaling_governor'ı oku, doğrula, sakla. Yakalanamazsa rollback olmaz (güvenli)."""
        if self._reversible_kind(command) != "governor":
            return
        try:
            cap = await self._executor.execute("cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor", timeout=10)
            prior = (cap.get("stdout", "") or "").strip().splitlines()
            prior_gov = prior[0].strip() if prior else ""
        except Exception:
            prior_gov = ""
        # GÜVENLİK: yalnız geçerli governor-adı sakla (komut-enjeksiyonu önle); aksi -> rollback yok.
        if prior_gov and _VALID_GOVERNOR.fullmatch(prior_gov):
            self._rollback_state[source] = {"kind": "governor", "state": prior_gov, "command": command}

    def _rollback_is_flapping(self, source: str) -> bool:
        """Anti-flapping: bu kaynak için son rollback _rollback_cooldown içinde mi (tekrar-tekrar
        geri-alma -> flapping). True -> rollback ATLA (doğrudan escalate)."""
        last = self._last_rollback.get(source)
        return last is not None and (time.monotonic() - last) < self._rollback_cooldown

    async def _attempt_rollback(self, source: str) -> tuple[bool, str]:
        """Yakalı reversible-state varsa geri-al. (rolled_back, rollback_result) döndür.
        Anti-flapping cooldown'da -> (False, 'skipped: flapping'). Yalnız mode=auto'dan çağrılır."""
        state = self._rollback_state.pop(source, None)
        if not state:
            return False, ""
        if self._rollback_is_flapping(source):
            return False, "skipped: flapping-cooldown"
        gov = state["state"]
        if not _VALID_GOVERNOR.fullmatch(gov):  # defense-in-depth (saklarken de doğrulandı)
            return False, "skipped: invalid-governor"
        q = shlex.quote(gov)
        cmd = f"cpufreq-set -g {q} 2>/dev/null || echo {q} | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null || true"
        # Codex P2: '|| true' + olası whitelist-eksikliği başarısızlığı maskeler → komut
        # exit_code'una GÜVENME. Rollback'i governor'ı RE-READ ederek DOĞRULA; gerçekten
        # geri dönmediyse rolled_back=False (gerçekleşmeyen rollback'i 'oldu' RAPORLAMA).
        try:
            await self._executor.execute(cmd, timeout=30)
            chk = await self._executor.execute("cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor", timeout=10)
            lines = (chk.get("stdout", "") or "").strip().splitlines()
            now_gov = lines[0].strip() if lines else ""
            ok = now_gov == gov
            res = f"governor={now_gov or '?'} (hedef {gov})"
        except Exception as e:
            ok = False
            res = f"rollback-error: {str(e)[:200]}"
        if ok:
            self._last_rollback[source] = time.monotonic()  # cooldown YALNIZ doğrulanmış rollback'te
            return True, res
        return False, f"rollback-DOĞRULANAMADI: {res}"

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
        provenance: str | None = None,
    ) -> None:
        """Kalıcı remediation ledger (server.db.remediation_log). Best-effort:
        DB yoksa/yazamazsa sessizce geç (remediation akışını bozma)."""
        if not self._db:
            return
        try:
            await self._db.execute(
                "INSERT INTO remediation_log "
                "(alert_source, severity, mode, action, command, executed, result, success, verify_status, provenance) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    provenance,
                ),
            )
        except Exception:
            pass

    async def _escalate_persistent(self) -> None:
        """Çözülmeyen critical alert'leri _escalation_interval'da yeniden bildir
        (okunmamış/ele-alınmamış critical sessizce unutulmasın). Re-ping = yeni
        escalation event -> notify-cron -> Telegram (aksiyon-önerili). Best-effort."""
        nowm = time.monotonic()
        for source, alert in list(self._active_alerts.items()):
            if alert.severity != "critical":
                continue
            # ilk-görülme: kaynak NEREDE yaratılırsa yaratılsın (_detect / _check_services
            # service:* / _check_vps vps:*) eskalasyon-saatini burada başlat -> interval
            # sonra re-escalate. (Codex P2: yalnız _detect-init metrik-dışı critical'leri
            # kaçırıyordu; tek-nokta uniform-init ile hepsi kapsanır.)
            if source not in self._last_escalation:
                self._last_escalation[source] = nowm
                continue
            elapsed = nowm - self._last_escalation[source]
            if elapsed < self._escalation_interval:
                continue
            # ACK-saygı: kullanıcı Telegram '✅ Gördüm' ile bu kaynağın son alert/
            # escalation event'ini onayladıysa YENİDEN BASMA (nag-etme). Skip ->
            # yeni unacked event yaratılmaz -> latest acked kalır -> sessiz (auto-resolve'a dek).
            if await self._source_acked(source):
                continue
            self._last_escalation[source] = nowm
            mins = int(elapsed / 60)
            try:
                await asyncio.to_thread(
                    emit_event,
                    type="alert",
                    source=f"escalation:{source}",
                    title=f"HÂLÂ AÇIK (~{mins}dk): {alert.message} — çözülmedi, manuel müdahale gerek",
                    severity="critical",
                    detail="Otonom remediation kapalı/yetmedi; bu kaynak hâlâ kritik eşikte.",
                )
            except Exception:
                pass

    async def _source_acked(self, source: str) -> bool:
        """Bu kaynağın EN SON alert/escalation event'i kullanıcı tarafından ACK'lendi mi
        (events.acked). Telegram '✅ Gördüm' butonu poller'da acked=1 yapar -> escalation
        durur. Best-effort (db yok/hata -> False = escalate-devam, fail-loud tercih)."""
        if not self._db:
            return False
        try:
            row = await self._db.fetch_one(
                "SELECT acked FROM events WHERE source IN (?, ?) ORDER BY id DESC LIMIT 1",
                (source, f"escalation:{source}"),
            )
            return bool(row and row.get("acked"))
        except Exception:
            return False

    # ── Read-only teşhis asistanı ──────────────────────────────

    def _maybe_diagnose(self, alert: Alert) -> None:
        """Sustained-critical alert için read-only LLM teşhis hipotezi spawn et (once/incident).
        KOMUT ÇALIŞTIRMAZ. Fail-silent — alert akışını asla bozmaz. asyncio.create_task ile
        tick'i bloklamaz (Ollama ~saniyeler sürebilir)."""
        if not self._diagnostic_enabled or alert.source in self._diagnosed:
            return
        self._diagnosed.add(alert.source)
        try:
            asyncio.create_task(self._diagnose_and_emit(alert))
        except RuntimeError:
            # event-loop yok (senkron test bağlamı) — sessizce atla
            self._diagnosed.discard(alert.source)

    async def _diagnose_and_emit(self, alert: Alert) -> None:
        """Read-only context topla → Ollama'ya kök-neden sor → diagnosis event'i emit et."""
        try:
            context = await asyncio.to_thread(self._gather_diag_context)
            hypothesis = await self._ask_diagnosis(alert, context)
            if not hypothesis:
                return
            await asyncio.to_thread(
                emit_event,
                type="alert",
                source=f"diagnosis:{alert.source}",
                title=f"🔍 Teşhis ({alert.source}): {hypothesis[:160]}",
                severity="warning",
                detail=(
                    f"Read-only LLM hipotezi ({self._diag_model}). "
                    f"Alert: {alert.message} (={alert.value}, eşik {alert.threshold}). "
                    f"KOMUT ÇALIŞTIRILMADI — doğrula.\n\n{hypothesis}"
                ),
            )
        except Exception:
            pass

    def _gather_diag_context(self) -> str:
        """Son 7 günde memory'ye kaydedilen değişiklikler (alert-korelasyonu).
        Logic paylaşılan RecentChangesProvider'a çıkarıldı (code-research de aynı
        kaynağı kullanır — duplikasyon-önleme); güncel _diag_memory_db ile delege."""
        from app.core.agents import RecentChangesProvider

        return RecentChangesProvider(self._diag_memory_db)._query()

    async def _ask_diagnosis(self, alert: Alert, context: str) -> str | None:
        """LLMCore ile kök-neden hipotezi sordur (timeout'lu, fail→None). Salt-okuma."""
        from app.core.agents.llmcore import llm_core

        prompt = (
            f"Sistem uyarısı: {alert.source} = {alert.value} (eşik {alert.threshold}). {alert.message}\n\n"
            f"Son 7 günde sistemde kaydedilen değişiklikler:\n{context}\n\n"
            "Bu uyarının MUHTEMEL kök nedenini 2-3 cümlede Türkçe tahmin et. Yukarıdaki "
            "değişikliklerden biriyle korelasyon görüyorsan açıkça belirt. Komut/aksiyon "
            "ÖNERME, sadece hipotez ver. Emin değilsen 'belirsiz' yaz."
        )
        out = await llm_core.generate(prompt, task="diagnosis", timeout=self._diag_timeout)
        return (out.strip()[:600] or None) if out else None

    # ── LIVESYS Faz 5 Slice-2: verify -> escalate ──────────────

    async def _verify_remediation(self, source: str) -> bool | None:
        """Aksiyon sonrası health re-check. True=düzeldi, False=hâlâ sorunlu,
        None=verify-edilemez (cpu sadece-log / belirsiz). Heuristik: cleanup etkisi
        gecikebilir -> False-fail mümkün (sonucu sadece escalate-notify, yıkıcı değil)."""
        base = source.split(":", 1)[0]
        try:
            if base == "service":
                svc = source.split(":", 1)[1]
                # shlex.quote = savunma-derinliği (refused adlar buraya ulaşamaz ama
                # source-string ileride başka üreticiden gelebilir — Codex P1 simetrisi).
                r = await self._executor.execute(f"systemctl is-active {shlex.quote(svc)}", timeout=10)
                return r.get("stdout", "").strip() == "active"
            if base == "docker":
                cont = source.split(":", 1)[1]
                # Codex P2: Running=true unhealthy'de de doğru -> false-recovery. Health-status'a
                # da bak: healthcheck'li container 'healthy' olmalı; healthcheck'siz (none) ->
                # Running yeter. Çıktı "<running>;<health|none>" (örn "true;healthy"/"true;none").
                r = await self._executor.execute(
                    f"docker inspect -f '{{{{.State.Running}}}};{{{{if .State.Health}}}}{{{{.State.Health.Status}}}}{{{{else}}}}none{{{{end}}}}' {shlex.quote(cont)}",
                    timeout=10,
                )
                parts = r.get("stdout", "").strip().lower().split(";")
                running = len(parts) >= 1 and parts[0] == "true"
                health = parts[1] if len(parts) >= 2 else "none"
                if not running or health == "unhealthy":
                    return False
                if health == "starting":
                    return None  # Codex P2: start_period geçici -> belirsiz, escalate ETME
                return True  # healthy veya healthcheck'siz (none)
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
        # INTERV: verify FAIL + reversible-state yakalı + flapping-değil -> AUTO-ROLLBACK
        # (önceki governor'a dön). Rollback ÇÖZÜM DEĞİL -> yine escalate (manuel müdahale).
        rolled_back = False
        rb_result = ""
        if escalated:
            rolled_back, rb_result = await self._attempt_rollback(source)
        else:
            # surer F1: verify-PASS'te de yakalı state'i TEMİZLE — yoksa bayat governor-state
            # sonraki (olası irreversible) aksiyonun verify-fail'inde yanıltıcı rollback tetikler.
            self._rollback_state.pop(source, None)
        if self._db:
            try:
                await self._db.execute(
                    "UPDATE remediation_log SET verify_status=?, escalated=?, rolled_back=?, rollback_result=? "
                    "WHERE alert_source=? AND verify_status IS NULL",
                    (status, 1 if escalated else 0, 1 if rolled_back else 0, rb_result or None, source),
                )
            except Exception:
                pass
        if escalated:
            # ESCALATE: otonom remediation çalıştı ama sorun sürüyor -> manuel müdahale.
            rb_note = f" [auto-rollback: {rb_result}]" if rolled_back else ""
            try:
                await asyncio.to_thread(
                    emit_event,
                    type="alert",
                    source=f"remediation:{source}",
                    title=f"Otonom remediation BAŞARISIZ: {source} hâlâ kritik — manuel müdahale gerek",
                    severity="critical",
                    detail=f"auto-remediation yürütüldü ama verify başarısız ({alert.message}).{rb_note}",
                )
            except Exception:
                pass

    # ── Slice-2: kullanıcı-onaylı tek-tıkla aksiyon ([🔧 Uygula]) ──────

    def _executable_playbook(self, source: str) -> list[dict] | None:
        """source -> ÇALIŞTIRILABİLİR playbook adımları (template doldurulmuş).
        None = bu kaynak için manuel-tetiklenebilir düzeltme yok (örn cpu = sadece-
        inceleme). escalation:/remediation: önekleri asıl kaynağa indirgenir."""
        for pfx in ("escalation:", "remediation:"):
            if source.startswith(pfx):
                source = source[len(pfx) :]
                break
        base, _, name = source.partition(":")
        # GÜVENLİK (defense-in-depth, RCE-yüzeyi): service/docker adı shell-komutuna
        # gömülüyor. Kaynak iç-yazımlı olsa da, herhangi bir gelecekteki injection
        # yolunu kapat -> yalnız güvenli unit/container-ad karakterleri. Aksi -> aksiyon yok.
        # TEK-KAYNAK: _VALID_UNIT (otonom _remediate_service/_container ile aynı desen;
        # eski _SAFE_UNIT_NAME kopyası birleştirildi — kopya-drift önleme).
        if base in ("service", "docker") and name and not _VALID_UNIT.fullmatch(name):
            return None
        if base == "service" and name:
            return [{"desc": f"Restart {name}", "cmd": f"systemctl restart {name}"}]
        if base == "docker" and name:
            # restart: durmuş container'ı da başlatır, unhealthy'yi de düzeltir (Codex P2;
            # 'docker start' çalışan-unhealthy'de no-op'tu).
            return [{"desc": f"Restart {name}", "cmd": f"docker restart {name}"}]
        if base == "cpu":
            return None  # cpu_critical sadece-log -> çalıştırılacak düzeltme yok
        steps = PLAYBOOKS.get(f"{base}_critical")
        return steps or None

    def has_actionable_playbook(self, source: str) -> bool:
        """notify-cron + endpoint: bu kaynağa [🔧 Uygula] sunulabilir mi."""
        return self._executable_playbook(source) is not None

    async def force_remediate(self, source: str) -> dict:
        """Kullanıcı [🔧 Uygula] ile AÇIK ONAY verdi -> remediation_mode-gate BYPASS
        (insan-in-loop ayrı gate; notify-default'ta bile çalışır çünkü onay manuel).
        Playbook'u yürüt + verify + ledger(mode='manual'). verify-fail -> escalate.
        Owner-auth ÇAĞRAN katmanda (telegram owner-chat / endpoint internal-key)."""
        for pfx in ("escalation:", "remediation:"):
            if source.startswith(pfx):
                source = source[len(pfx) :]
                break
        steps = self._executable_playbook(source)
        if steps is None:
            return {"ok": True, "executed": False, "reason": "no_actionable_playbook", "source": source}

        results = []
        all_ok = True
        for step in steps:
            try:
                r = await self._executor.execute(step["cmd"], timeout=30)
                ok = r.get("exit_code", 1) == 0
                out = r.get("stdout", "")[:300]
            except Exception as e:
                ok = False
                out = str(e)[:300]
            all_ok = all_ok and ok
            results.append({"action": step["desc"], "success": ok})
            await self._persist_remediation_row(
                source,
                "critical",
                "manual",
                step["desc"],
                step["cmd"],
                executed=True,
                result=out,
                success=ok,
                verify_status=None,
            )

        # verify (kısa grace -> cleanup/restart etkisi otursun)
        if self._verify_grace:
            await asyncio.sleep(self._verify_grace)
        verified = await self._verify_remediation(source)
        status = "n/a" if verified is None else ("pass" if verified else "fail")
        if self._db:
            try:
                await self._db.execute(
                    "UPDATE remediation_log SET verify_status=? WHERE alert_source=? AND mode='manual' AND verify_status IS NULL",
                    (status, source),
                )
            except Exception:
                pass
        if status == "fail":
            # düzeltme çalıştı ama sorun sürüyor -> yeni unacked critical -> escalate.
            try:
                await asyncio.to_thread(
                    emit_event,
                    type="alert",
                    source=f"remediation:{source}",
                    title=f"Manuel remediation BAŞARISIZ: {source} hâlâ kritik — elle müdahale gerek",
                    severity="critical",
                    detail="Kullanıcı [🔧 Uygula] ile çalıştırdı ama verify başarısız.",
                )
            except Exception:
                pass
        return {
            "ok": True,
            "executed": True,
            "source": source,
            "steps": results,
            "all_success": all_ok,
            "verify": status,
        }

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
                # GÜVENLİK (Codex P1): ad-doğrulama PROBE'dan önce — probe da f-string ile
                # tam-shell'e gider, remediate'teki guard tek başına yetmez (enjeksiyonlu
                # ad probe'da çalışırdı). Geçersiz ad shell'e HİÇ gömülmez; sessiz değil:
                # alarm yolu akar, _remediate_service refused-satırı+webhook yazar.
                if not _VALID_UNIT.fullmatch(svc):
                    problem = f"Service adi gecersiz ({svc[:60]!r}) — izlenemiyor (enjeksiyon riski)"
                else:
                    result = await self._executor.execute(f"systemctl is-active {shlex.quote(svc)}", timeout=5)
                    problem = None if result.get("stdout", "").strip() == "active" else f"Service {svc} is not active"
                if problem:
                    source = f"service:{svc}"
                    if source not in self._active_alerts:
                        alert = Alert(
                            id=f"{source}-{self._check_count}",
                            severity="critical",
                            source=source,
                            message=problem,
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
                # GÜVENLİK (Codex P1): servis yoluyla simetrik — doğrulama probe'dan önce.
                if not _VALID_UNIT.fullmatch(container):
                    down, unhealthy, status = True, False, ""
                    invalid_msg = f"Container adi gecersiz ({container[:60]!r}) — izlenemiyor (enjeksiyon riski)"
                else:
                    invalid_msg = None
                    result = await self._executor.execute(
                        f"docker ps --filter name={shlex.quote(container)} --format '{{{{.Status}}}}'", timeout=5
                    )
                    status = result.get("stdout", "").strip()
                    # Codex P2: 'Up (unhealthy)' de 'Up' içerir -> çalışıyor-ama-unhealthy kaçardı.
                    # Healthcheck'li container (n8n/qdrant) unhealthy = kritik outage -> yakala.
                    down = not status or "Up" not in status
                    unhealthy = "unhealthy" in status.lower()
                if down or unhealthy:
                    source = f"docker:{container}"
                    if source not in self._active_alerts:
                        msg = invalid_msg or (
                            f"Container {container} is not running" if down else f"Container {container} UNHEALTHY ({status})"
                        )
                        alert = Alert(
                            id=f"{source}-{self._check_count}",
                            severity="critical",
                            source=source,
                            message=msg,
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
                "StrictHostKeyChecking=accept-new",
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

    async def _local_internet_up(self) -> bool:
        """Whether klipper itself has outbound internet right now.

        Used to disambiguate a failed VPS SSH probe: if our own WAN is down, the
        failure is local, not a VPS outage. Tries a short TCP connect to public
        anycast resolvers; any success → internet up. ICMP is avoided (often
        filtered, needs the ping binary); a raw TCP connect needs no privileges.
        """
        for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53)):
            try:
                fut = asyncio.open_connection(host, port)
                _, writer = await asyncio.wait_for(fut, timeout=3)
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
                return True
            except (TimeoutError, OSError):
                continue
        return False

    async def _check_vps(self) -> None:
        """Collect VPS host metrics + container state via the SSH probe, persist, alert."""
        now = datetime.now(UTC).isoformat()
        probe = await self._vps_ssh_probe()

        if probe is None:
            self._vps_probe_fails += 1
            await self._store_vps_metrics({}, online=False)
            self._latest_vps = {"online": False, "timestamp": now}
            # SUSTAINED-GATE: tek geçici probe-fail (SSH timeout / anlık blip / VPS-busy)
            # ANINDA alert üretmesin — N ardışık-fail = gerçek kesinti. Tek-blip → bekle,
            # sıradaki tick'te retry. (metrik-alarmlarındaki _sustained_high simetrisi.)
            if self._vps_probe_fails < self._vps_fail_threshold:
                return
            # Disambiguate before blaming the VPS: an SSH-probe failure during a
            # local internet outage means *klipper's own WAN* dropped, not that the
            # VPS is down. Without this, every klipper ISP/DNS hiccup produced a
            # false "vps:offline" alert storm (2026-06-17 ~1h WAN blip incident).
            if not await self._local_internet_up():
                source = "klipper:wan-down"
                if source not in self._active_alerts:
                    self._active_alerts[source] = Alert(
                        id=f"{source}-{self._check_count}",
                        severity="critical",
                        source=source,
                        message="klipper has no outbound internet — VPS reachability unknown",
                        value=0,
                        threshold=1,
                        timestamp=now,
                    )
                return
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

        self._vps_probe_fails = 0  # başarılı probe → ardışık-fail sayacı sıfır
        await self._store_vps_metrics(probe, online=True)
        self._latest_vps = {**probe, "online": True, "timestamp": now}

        # Auto-resolve VPS offline / WAN-down alerts: a successful probe proves both
        # the VPS *and* our own internet are up.
        for resolved in ("vps:offline", "klipper:wan-down"):
            self._active_alerts.pop(resolved, None)

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
        # GÜVENLİK: ad-doğrulama remediation'dan ÖNCE (f-string → tam-shell yolu;
        # _refuse_invalid_unit ledger'a görünür 'refused' satırı yazar, alarm akar).
        if not _VALID_UNIT.fullmatch(service):
            await self._refuse_invalid_unit(source, alert, "service", service)
            return
        # taze-boot bug fix (Codex-CI): get(source,0)+monotonic<cooldown erken-return
        # yapardi -> None-check (devops _remediate ile ayni).
        last = self._cooldowns.get(source)
        if last is not None and (now - last) < self._cooldown_seconds:
            return
        self._cooldowns[source] = now

        # mode-gate (Codex P1): notify/dry_run'da systemctl restart YÜRÜTÜLMEZ.
        # shlex.quote = savunma-derinliği (doğrulama geçse bile meta-karakter etkisiz).
        await self._apply_remediation(alert, source, f"Restart {service}", f"systemctl restart {shlex.quote(service)}", timeout=15)
        await self._send_webhook(alert)
        await self._verify_and_escalate(source, alert)

    async def _refuse_invalid_unit(self, source: str, alert: Alert, kind: str, name: str) -> None:
        """Geçersiz servis/konteyner adı → remediation REFUSED (yürütme yok), ama
        SESSİZ DEĞİL: ledger'a refused satırı + webhook (görünürlük korunur).
        Ad config'ten gelir; geçersiz ad = config bozuk/oynanmış → incelenmeli."""
        msg = f"refused: gecersiz {kind} adi ({name[:60]!r}) — komut-enjeksiyonu riski, yurutme yok"
        self._remediation_log.append(
            RemediationRecord(
                timestamp=datetime.now(UTC).isoformat(),
                alert_source=source,
                action=f"Restart {kind} REFUSED",
                command="(yurutulmedi)",
                result=msg,
                success=False,
            )
        )
        await self._persist_remediation_row(
            source,
            alert.severity,
            self._remediation_mode,
            f"Restart {kind} REFUSED",
            "(yurutulmedi)",
            False,
            msg,
            False,
            verify_status="refused",
            provenance=provenance_json(alert, self._remediation_mode, detected_at=getattr(alert, "timestamp", None) or None),
        )
        alert.remediation = f"[refused] {msg}"
        await self._send_webhook(alert)

    async def _remediate_container(self, container: str, alert: Alert) -> None:
        now = time.monotonic()
        source = f"docker:{container}"
        # GÜVENLİK: ad-doğrulama (servis yoluyla simetrik — f-string → tam-shell).
        if not _VALID_UNIT.fullmatch(container):
            await self._refuse_invalid_unit(source, alert, "container", container)
            return
        # taze-boot bug fix (Codex-CI): get(source,0)+monotonic<cooldown erken-return
        # yapardi -> None-check (devops _remediate ile ayni).
        last = self._cooldowns.get(source)
        if last is not None and (now - last) < self._cooldown_seconds:
            return
        self._cooldowns[source] = now

        # mode-gate (Codex P1): notify/dry_run'da YÜRÜTÜLMEZ. restart: durmuş+unhealthy
        # ikisini de kapsar (Codex P2). shlex.quote = savunma-derinliği.
        await self._apply_remediation(alert, source, f"Restart {container}", f"docker restart {shlex.quote(container)}", timeout=15)
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
            # FORMAT-AGNOSTİK zaman filtresi (Codex P2). metrics_history.timestamp Python
            # isoformat() ile ISO-T ('T'-ayraçlı, +00:00) yazılır; AMA schema DEFAULT'u
            # (database.py) datetime('now') = BOŞLUK-ayraçlı → timestamp atlanırsa boşluk-
            # satır oluşur. Ham string-compare iki formatı karıştırır ('T'(0x54) vs ' '(0x20))
            # → yanlış pencere (ya hep-içeri ya boşluk-satırı-dışla). datetime(timestamp) HER
            # İKİ formatı UTC'ye normalize eder → doğru, format-bağımsız pencere.
            # WHERE + ORDER BY ikisi de datetime(timestamp) → expression index idx_metrics_dt
            # RANGE-SEARCH sağlar (Codex P2 #2: aksi halde pencere<500 satırda full-SCAN +
            # temp-sort). database.py'de tanımlı.
            """SELECT * FROM metrics_history
               WHERE datetime(timestamp) > datetime('now', ?)
               ORDER BY datetime(timestamp) DESC LIMIT 500""",
            (f"-{minutes} minutes",),
        )
        return [dict(r) for r in rows]

    async def get_vps_metrics_history(self, minutes: int = 60) -> list[dict]:
        if not self._db:
            return []
        rows = await self._db.fetch_all(
            # Format-agnostik + expression index idx_vps_metrics_dt — bkz get_metrics_history.
            """SELECT * FROM vps_metrics_history
               WHERE datetime(timestamp) > datetime('now', ?)
               ORDER BY datetime(timestamp) DESC LIMIT 500""",
            (f"-{minutes} minutes",),
        )
        return [dict(r) for r in rows]
