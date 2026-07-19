"""DevOpsAgent metrics mixin — split from monolithic devops_agent.py."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from app.core.config import read_env_var
from app.core.devops._base import _DevOpsAgentBase
from app.core.devops.models import (
    _SUSTAINED_N,
    _SUSTAINED_SOURCES,
    Alert,
)
from app.core.events import emit_event

log = logging.getLogger("devops_agent")


def _parse_utc_timestamp(value: Any) -> datetime | None:
    """Parse the timestamp formats written by this service without losing microseconds."""
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class MetricsMixin(_DevOpsAgentBase):
    """DevOpsAgent metrics mixin — split from monolithic devops_agent.py."""

    async def _store_metrics(self, metrics: dict[str, Any]) -> None:
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
            # #1334: bu satır önceden sessizdi -> meta-monitor "dead" alarmı atarken
            # kök-neden görünmezdi (85dk-lik 07-14 kesintisi log-izsiz kaldı). Artık log'lu.
            log.warning("metrics_history insert failed", exc_info=True)

    def _is_in_cpu_grace_window(self) -> bool:
        """CPU-grace penceresi 03:00-05:00 UTC (= 06:00-08:00 Europe/Istanbul; cron LOCAL koşar).
        Bu pencerede meşru CPU-ağır cron'lar koşar → tek-örnek/sustained %95 = FP, alarm bastırılır
        (klipper #100224 FP-fix). GERÇEK sebep (review-düzeltme): test-runner (06:00 local=03:00 UTC,
        tam pytest suite) + e2e-live-test (07:00 local=04:00 UTC) + system-state (07:30). NOT: daily-
        backup 03:00 LOCAL=00:00 UTC ve hafif (~4sn) → bu pencerede DEĞİL; CPU-FP sebebi O değil.
        Override: CPU_GRACE_START_HOUR / CPU_GRACE_END_HOUR env-var (UTC saat, int)."""
        try:
            start = int(read_env_var("CPU_GRACE_START_HOUR") or "3")
            end = int(read_env_var("CPU_GRACE_END_HOUR") or "5")
        except (ValueError, TypeError):
            return False
        return start <= datetime.now(UTC).hour < end

    def _sustained_high(self, key: str, threshold: float) -> bool:
        """Son _SUSTAINED_N örnek (current dahil — _history'ye _detect'ten ÖNCE append edilir)
        eşik-üstü mü → sürdürülen-yük. Geçici zirveyi (zamanlanmış toplu-iş) filtreler.
        Yeterli geçmiş yoksa (<N örnek, startup) False — sürdürülen doğrulanamaz, critical etme."""
        recent = list(self._history)[-_SUSTAINED_N:]
        vals = [v for m in recent if (v := m.get(key)) is not None]
        if len(vals) < _SUSTAINED_N:
            return False
        return all(v >= threshold for v in vals)

    def _detect(self, metrics: dict[str, Any]) -> list[Alert]:
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

            # klipper #100224: CPU-grace penceresi (03:00-05:00 UTC = 06:00-08:00 local).
            # test-runner (06:00) + e2e-live-test (07:00) meşru CPU %95+ yapıyor — FP önleme.
            # Diğer metrikler (disk/mem/temp) bu pencerede yine izlenir; runaway-PROCESS'leri
            # agent_watchdog ayrı tespit eder (pencere kör-nokta değil).
            if source == "cpu" and self._is_in_cpu_grace_window():
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

    def _auto_resolve(self, metrics: dict[str, Any]) -> None:
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
        except Exception as e:
            # disc#1353b: 07-18'de 6-saatlik DB-lock penceresinde alert-kayıtları SESSİZCE
            # kayboldu (n8n-down görünmedi) — yutma yerine tip-adı logu (payload'sız).
            log.warning("alerts insert failed (%s): %s", alert.source, type(e).__name__)
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
        except Exception as e:
            # disc#1353b: events-köprüsü kopunca Telegram-hattı sessiz kalıyordu — logla.
            log.warning("events bridge failed (%s): %s", alert.source, type(e).__name__)

    async def _resolve_alert_db(self, alert: Alert) -> None:
        await self._resolve_alert_db_by_source(alert.source, alert.resolved_at or alert.timestamp)

    async def _resolve_alert_db_by_source(self, source: str, healthy_at: str) -> None:
        """DB'deki açık alert-satırlarını KAYNAK-bazlı kapat — in-memory Alert nesnesi gerekmeden.
        Codex-P2 (PR#340 follow-up): alert DB'ye yazıldıktan sonra healthy-probe gelmeden restart
        olursa _active_alerts boşalır; resolve-yolu in-memory nesneye bağlı kalırsa DB satırı
        SONSUZA DEK resolved=0 kalır (stuck-open, kullanıcıdan gizli). Idempotent UPDATE
        (açık satır yoksa no-op) — healthy-yoldan koşulsuz çağrılabilir.

        Codex-P2 (PR#342): ZAMAN-SINIRI şart. Bu resolve her healthy-probe'da (aktif-alert yokken
        bile) kuyruğa girdiğinden, DB-lock/çok-worker penceresinde GECİKMİŞ bir healthy-task,
        SONRAKİ bir outage'ın yeni resolved=0 satırını yanlışlıkla kapatıp gizleyebilirdi. Yalnız
        healthy-örnekleme-anından ÖNCE (veya eşit) görülmüş alert'ler kapatılır — sonraki outage
        (timestamp > healthy_at) dokunulmaz.

        Codex-P2 (PR#343 r2): SQLite datetime() saniye-altını kırpıyor, julianday() ise
        mikro-saniyeleri güvenilir biçimde ayıramıyor. Aday satırlar Python'da UTC-aware ve tam
        mikro-saniye hassasiyetiyle seçilir; UPDATE yalnız seçilmiş id'lere uygulanır. SELECT'ten
        sonra eklenen yeni outage bu nedenle yanlışlıkla kapanamaz."""
        if not self._db:
            return
        healthy_time = _parse_utc_timestamp(healthy_at)
        if healthy_time is None:
            log.warning("alert resolve-update skipped (%s): invalid healthy timestamp", source)
            return
        try:
            rows = await self._db.fetch_all(
                "SELECT id, timestamp FROM alerts WHERE source = ? AND resolved = 0",
                (source,),
            )
            eligible_ids: list[int] = []
            invalid_timestamps = 0
            for row in rows:
                alert_time = _parse_utc_timestamp(row.get("timestamp"))
                row_id = row.get("id")
                if alert_time is None or not isinstance(row_id, int):
                    invalid_timestamps += 1
                    continue
                if alert_time <= healthy_time:
                    eligible_ids.append(row_id)

            if invalid_timestamps:
                log.warning(
                    "alert resolve-update skipped invalid rows (%s): %d",
                    source,
                    invalid_timestamps,
                )

            # Stay below SQLite's parameter limit even if legacy stuck-open rows accumulated.
            for offset in range(0, len(eligible_ids), 400):
                batch = eligible_ids[offset : offset + 400]
                placeholders = ", ".join("?" for _ in batch)
                await self._db.execute(
                    f"UPDATE alerts SET resolved = 1, resolved_at = ?, invalid_at = ? WHERE resolved = 0 AND id IN ({placeholders})",
                    (healthy_at, healthy_at, *batch),
                )
        except Exception as e:
            # disc#1353b: resolve-güncellemesi kaybolursa alert DB'de sonsuza dek açık görünür.
            log.warning("alert resolve-update failed (%s): %s", source, type(e).__name__)

    @property
    def active_alerts(self) -> list[dict[str, Any]]:
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
    def metrics_buffer(self) -> list[dict[str, Any]]:
        return list(self._history)

    async def get_alerts_history(self, limit: int = 50, severity: str | None = None) -> list[dict[str, Any]]:
        if not self._db:
            return []
        query = "SELECT * FROM alerts ORDER BY timestamp DESC LIMIT ?"
        params: tuple[Any, ...] = (limit,)
        if severity:
            query = "SELECT * FROM alerts WHERE severity = ? ORDER BY timestamp DESC LIMIT ?"
            params = (severity, limit)
        rows = await self._db.fetch_all(query, params)
        return [dict(r) for r in rows]

    async def get_metrics_history(self, minutes: int = 30) -> list[dict[str, Any]]:
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
