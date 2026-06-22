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

# OPERASYONEL-ZEMİN: istatistiksel-anomali (z) YETMEZ — değer gerçekten concern-seviyesinde
# olmalı. Yoksa session-activity benign-spike = FP (klipper gözlem 2026-06-22: cpu 0.6→6.3
# z=6.4 ama %6 önemsiz; mem 19→35 z=11.1 ama %35 sağlıklı → 3 gereksiz warn-page/2h).
# {metric: (high_floor, low_floor)}. low_floor=None → DÜŞÜK-yön suppress: resource metriklerinde
# (cpu/mem/disk/temp) düşük-değer benign (idle/boş/serin), crash-sinyali DEĞİL (crash=liveness'in
# işi). Bilinmeyen-metrik → floor-yok (z-tek-başına, geriye-uyumlu). devops-static-eşik'i tamamlar.
ANOMALY_FLOORS: dict[str, tuple[float, float | None]] = {
    "cpu_usage": (70.0, None),
    "memory_usage": (80.0, None),
    "disk_usage": (85.0, None),
    "temperature": (75.0, None),
}

# PERSISTENCE-GATE: floor-üstü tek-örnek (transient) spike page üretmesin — metrik floor'u
# son N ARDIŞIK-tick'te de aşmalı (geçici-tepe ≠ sürekli-sorun). Klipper gözlem 2026-06-22:
# kendi research/run sorgum mem'i 1-tick %91'e fırlattı (z=60.8, floor-üstü=GERÇEK ama anında
# %16'ya döndü) ve warn-page üretti. Sürekli-yüksek (gerçek-sorun) çoklu-tick floor-üstü kalır;
# transient kalmaz → bu gate yalnız transient'i eler, sürekli-anomaliyi geçirir.
# Worker-duplikasyonu (2 uvicorn worker aynı saniye 2 satır yazar) → saniye-granülünde GROUP BY
# ile tick-bazına indirilir (satır-bazı DEĞİL). FAIL-OPEN: yeterli-veri yoksa GEÇİR (page-kaçırma
# > fazladan-page; watchdog-FP-disiplininin tersi — burada false-negative daha kötü).
ANOMALY_PERSIST_SAMPLES = 2  # floor'u aşması gereken ardışık-distinct-tick sayısı
ANOMALY_PERSIST_WINDOW_MIN = 30  # persistence-lookback (dk; yakın-geçmiş, gap'e dayanıklı)


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
            # datetime(timestamp): metrics_history ISO-T yazılır (monitor_agent
            # datetime.now(UTC).isoformat()), datetime('now') boşluk-ayraçlı → çıplak
            # string-karşılaştırma LEKSİK ('T'>' ') = pencere bozuk (Codex #199). datetime()
            # her iki tarafı normalize eder → TEMPORAL karşılaştırma.
            rows = con.execute(
                f"SELECT {cols} FROM metrics_history WHERE datetime(timestamp) > datetime('now', ?) ORDER BY id",
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


def _persisted_beyond_floor(
    metric: str,
    high_floor: float,
    low_floor: float | None,
    is_high: bool,
    *,
    persist: int = ANOMALY_PERSIST_SAMPLES,
    window_min: int = ANOMALY_PERSIST_WINDOW_MIN,
) -> bool:
    """Son `persist` ardışık-distinct-tick metrik floor'u (high) aşıyor / (low) altında mı?
    True = sürekli (page-et), False = transient (ele). Worker-duplikasyonu saniye-granülü
    GROUP BY ile tick'e indirilir. FAIL-OPEN: yeterli-tick yok / floor-yok / DB-hata → True
    (page-kaçırmaktansa fazladan-page). Bilinmeyen-metrik (high_floor=-inf) → trivial True
    (z-only geriye-uyumlu, persistence uygulanmaz)."""
    # Floor-yok metrik (high=-inf veya low=None/inf) → persistence anlamsız, geçir.
    if is_high and high_floor == float("-inf"):
        return True
    if not is_high and (low_floor is None or low_floor == float("inf")):
        return True
    try:
        con = get_conn(server_db_path(), readonly=True)
        try:
            rows = con.execute(
                # Saniye-granülü GROUP BY: 2-worker aynı-tick satırlarını tek tick'e indirir
                # (AVG). Son `persist` distinct-tick (yeni→eski).
                f"SELECT AVG({metric}) AS v FROM metrics_history "  # noqa: S608 (metric ∈ METRICS sabiti)
                f"WHERE datetime(timestamp) > datetime('now', ?) AND {metric} IS NOT NULL "
                "GROUP BY datetime(timestamp) ORDER BY datetime(timestamp) DESC LIMIT ?",
                (f"-{int(window_min)} minutes", int(persist)),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        logger.exception("anomaly _persisted_beyond_floor sorgu hatası (fail-open → geçir)")
        return True
    vals = [float(r["v"]) for r in rows if r["v"] is not None]
    if len(vals) < persist:
        return True  # yeterli-tick yok (yeni-başladı/gap) → fail-open, geçir
    if is_high:
        return all(v >= high_floor for v in vals)
    return all(v <= low_floor for v in vals)  # type: ignore[operator]  # low_floor None yukarıda elendi


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
        # Operasyonel-zemin guard: istatistiksel-anomali + değer concern-seviyesinde olmalı.
        high_floor, low_floor = ANOMALY_FLOORS.get(metric, (float("-inf"), float("inf")))
        if z > 0 and latest < high_floor:
            continue  # yüksek-yön ama önemsiz-değer (örn cpu %6 < %70) → FP, atla
        if z < 0 and (low_floor is None or latest > low_floor):
            continue  # düşük-yön: resource-low benign (None) veya floor-üstü → atla
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
    persist: int = ANOMALY_PERSIST_SAMPLES,
) -> dict[str, int]:
    """Tek tur: metrics_history → robust-anomali → persistence-gate → emit_throttled(type=anomaly, warn).
    Fail-safe. `series` verilirse DB-okuma atlanır (test-injection). persistence-gate transient-spike'ı
    eler (floor son `persist` ardışık-tick'te aşılmalı; fail-open). Döndürür: {anomalies, emitted, suppressed, transient}."""
    summary: dict[str, int] = {"anomalies": 0, "emitted": 0, "suppressed": 0, "transient": 0}
    try:
        if not _enabled():
            return summary
        src = series if series is not None else _read_metric_series(hours)
        anomalies = detect_anomalies(src, min_samples=min_samples, threshold=threshold)
        summary["anomalies"] = len(anomalies)
        for a in anomalies:
            # Persistence-gate: floor-üstü tek-tick (transient) ise ele (gerçek-sürekli geçer).
            high_floor, low_floor = ANOMALY_FLOORS.get(a["metric"], (float("-inf"), float("inf")))
            is_high = a["direction"] == "yüksek"
            if not _persisted_beyond_floor(a["metric"], high_floor, low_floor, is_high, persist=persist):
                summary["transient"] += 1
                logger.info("anomaly transient-elendi (persistence): %s latest=%s z=%s", a["metric"], a["latest"], a["robust_z"])
                continue
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
