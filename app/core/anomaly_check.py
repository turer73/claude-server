"""Gap-4 ingestion-producer: dinamik/istatistiksel metrik-anomali (static-eşik ötesi) → events-spine.

devops_agent STATİK-eşik izliyor (cpu>X, temp>Y); bu producer ADAPTİF: `metrics_history`'den
robust-baseline (median + MAD) hesaplar, son-değer baseline'dan >THRESHOLD MAD saparsa anomali
= "bu-davranış-için-olağandışı" (static-eşik-altı kalan sapmaları yakalar, devops'u TAMAMLAR).
→ `emit_event(type="anomaly", severity="warn")`.

NEDEN robust-statistical (river/ML DEĞİL — MVP, awareness-research gap-4 "river VEYA S-ESD"):
median + MAD pure-Python (dep-yok), test-edilebilir, robust (outlier-dayanıklı). Cömert MAD-eşik
(watchdog FP-disiplini: yanlış-pozitif "felaketten beter"). river/S-ESD/hour-bucket-seasonal =
follow-up upgrade (diurnal-pattern FP'sini azaltır; MVP recent-window + cömert-eşik ile yönetilir).

emit_throttled (gap-2 helper, 3. gerçek tüketici → DRY): persistent-anomali (metrik takılı-yüksek)
her cron-turunda RE-EMIT etmez → 30dk pencerede bastır. severity=warn (gap-2 #100139 dersi).
"""

from __future__ import annotations

import logging
import sqlite3
import statistics
from typing import Any

from app.core.config import read_env_var
from app.core.emit_throttle import emit_throttled
from app.db.data_layer import get_conn, server_db_path

logger = logging.getLogger(__name__)

# metrics_history sayısal sütunları (load_avg/network_io TEXT → atla).
METRICS: tuple[str, ...] = ("cpu_usage", "memory_usage", "disk_usage", "temperature")
ANOMALY_WINDOW_HOURS = 24  # baseline-pencere (son N saat)
ANOMALY_MIN_SAMPLES = 12  # bu kadar örnek yoksa atla (güvenilmez baseline → sahte-anomali önleme)
ANOMALY_MAD_THRESHOLD = 5.0  # robust-z >= bu = anomali (CÖMERT; diurnal-FP önleme)
ANOMALY_DEDUP_SECONDS = 1800.0  # 30dk (persistent-anomali re-emit-yok)
_MAD_SCALE = 0.6745  # robust-z normalizasyon sabiti (normal-dağılım MAD→sigma)


def _enabled() -> bool:
    """Kill-switch (default ON). read_env_var (#174 sınıfı; early-return'de kullanılır)."""
    return (read_env_var("ANOMALY_CHECK_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")


def _read_metric_series(hours: int = ANOMALY_WINDOW_HOURS) -> dict[str, list[float]]:
    """metrics_history son N-saat → {metric: [değerler] (eski→yeni)}. None'lar atlanır.
    Hata → {} (fail-safe; cron-bozmaz)."""
    cols = ", ".join(METRICS)
    try:
        con = get_conn(server_db_path(), readonly=True)
        try:
            rows = con.execute(
                f"SELECT {cols} FROM metrics_history WHERE timestamp > datetime('now', ?) ORDER BY id",
                (f"-{int(hours)} hours",),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        logger.exception("anomaly _read_metric_series sorgu hatası (fail-safe)")
        return {}
    series: dict[str, list[float]] = {m: [] for m in METRICS}
    for row in rows:
        for m in METRICS:
            v = row[m]
            if v is not None:
                series[m].append(float(v))
    return series


def robust_zscore(baseline: list[float], latest: float) -> float | None:
    """median + MAD robust-z (latest'in baseline'a göre sapması). MAD=0 (varyans-yok) → None
    (tespit-yok; flat-baseline'da sahte-anomali üretme). |z| büyük = olağandışı."""
    if len(baseline) < 2:
        return None
    med = statistics.median(baseline)
    mad = statistics.median([abs(v - med) for v in baseline])
    if mad == 0:
        return None
    return _MAD_SCALE * (latest - med) / mad


def detect_anomalies(
    series: dict[str, list[float]], *, min_samples: int = ANOMALY_MIN_SAMPLES, threshold: float = ANOMALY_MAD_THRESHOLD
) -> list[dict[str, Any]]:
    """Her metrik için son-değer baseline'dan >=threshold MAD sapıyor mu? Anomali listesi.
    baseline = latest-HARİÇ pencere (latest'i kendi-baseline'ına karıştırma)."""
    out: list[dict[str, Any]] = []
    for metric, vals in series.items():
        if len(vals) < min_samples:
            continue
        latest = vals[-1]
        baseline = vals[:-1]
        z = robust_zscore(baseline, latest)
        if z is None or abs(z) < threshold:
            continue
        med = statistics.median(baseline)
        direction = "yüksek" if z > 0 else "düşük"
        out.append(
            {
                "metric": metric,
                "latest": round(latest, 2),
                "median": round(med, 2),
                "robust_z": round(z, 1),
                "direction": direction,
                "detail": f"{metric}={latest:.1f} olağandışı-{direction} (z={z:.1f}, median={med:.1f}, eşik={threshold:.0f}MAD)",
            }
        )
    return out


def run_anomaly_check(
    hours: int = ANOMALY_WINDOW_HOURS,
    min_samples: int = ANOMALY_MIN_SAMPLES,
    threshold: float = ANOMALY_MAD_THRESHOLD,
    series: dict[str, list[float]] | None = None,
) -> dict[str, int]:
    """Tek tur: metrics_history → robust-anomali → emit_throttled(type=anomaly, warn).
    Fail-safe. `series` verilirse DB-okuma atlanır (test-injection). Döndürür: {anomalies, emitted, suppressed}."""
    summary: dict[str, int] = {"anomalies": 0, "emitted": 0, "suppressed": 0}
    try:
        if not _enabled():
            return summary
        src = series if series is not None else _read_metric_series(hours)
        anomalies = detect_anomalies(src, min_samples=min_samples, threshold=threshold)
        summary["anomalies"] = len(anomalies)
        for a in anomalies:
            res = emit_throttled(
                type="anomaly",
                source=f"anomaly:{a['metric']}",
                title=f"metrik-anomali: {a['metric']} olağandışı-{a['direction']} (z={a['robust_z']})",
                severity="warn",
                detail=str(a["detail"]),
                payload=a,
                window_seconds=ANOMALY_DEDUP_SECONDS,
            )
            if res.emitted:
                summary["emitted"] += 1
            elif res.suppressed:
                summary["suppressed"] += 1
    except Exception:
        logger.exception("anomaly-check hatası (fail-safe)")
    return summary
