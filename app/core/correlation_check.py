"""Gap-5 aggregation-producer: cross-source event korelasyonu → incident (keep-pattern).

LSA üreticileri (gap-2/3/4/7/8) bağımsız sinyal emit eder; gap-5 onları events-spine'da
OKUR ve zaman-penceresinde birlikte-oluşan FARKLI-kaynak sinyalleri tek "incident"e gruplar
(keephq/keep deseni: fingerprint-dedup + zaman-pencereli incident-grupla). N ayrı warn-page
yerine "şu N sinyal ilişkili = muhtemelen tek-kök-neden" → fatigue↓ + içgörü↑.

KAPSAM: yalnız sistem-sağlık sinyalleri korele edilir (exception/anomaly/drift/log-novelty +
watchdog:*). code-review (ayrı review-akışı), job-outcome/intent-liveness (rutin) HARİÇ —
korelasyon-gürültüsü önleme. Kendi çıktısı (type=incident) de HARİÇ (recursive-önleme).

emit_throttled (gap-2 helper, 4. tüketici): aynı incident (aynı kaynak-kümesi) 30dk pencerede
re-emit edilmez. severity=warn (gap-2 #100139). Fail-safe (cron-bozmaz). Min 3 farklı-kaynak
(2026-06-30 eval: 2-kaynak coincidental-FP elendi — aşağı bkz CORRELATION_MIN_SOURCES).
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from typing import Any

from app.core.config import read_env_var
from app.core.emit_throttle import emit_throttled
from app.db.data_layer import get_conn, server_db_path

logger = logging.getLogger(__name__)

CORRELATION_WINDOW_MIN = 15  # birlikte-oluşma penceresi (keep deseni: 15dk)
# >= bu kadar FARKLI kaynak = korele incident. 2→3 (2026-06-30 #1152 eval, canlı-veri):
# tarihsel 23 incident'in 22'si (96%) kronik 'drift:sha + watchdog:heartbeat:last-code-review'
# çiftiydi = coincidental-FP (heartbeat flood'u PR#229'da düzeldi; drift:sha kalıcı always-on,
# 3günde 99). 2-kaynak eşiği "her-zaman-orada drift + tek-geçici-sinyal = incident" sahte-FP
# motoruydu. Tek GERÇEK incident 5+ kaynaklıydı (exception:OperationalError + log-novelty x4) —
# min-3 onu korur. min-3 = yapısal-FP-eler + gelecekteki kronik-kaynaklara karşı dayanıklı.
CORRELATION_MIN_SOURCES = 3
CORRELATION_DEDUP_SECONDS = 1800.0  # 30dk (aynı kaynak-kümesi re-emit-yok)
# Korele edilen sistem-sağlık sinyal-tipleri (rutin/review gürültüsü hariç). watchdog source ile.
SIGNAL_TYPES: tuple[str, ...] = ("exception", "anomaly", "drift", "log-novelty")


def _enabled() -> bool:
    """Kill-switch (default ON). read_env_var (#174 sınıfı; early-return'de kullanılır)."""
    return (read_env_var("CORRELATION_CHECK_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")


def _read_signal_events(window_min: int = CORRELATION_WINDOW_MIN) -> list[dict[str, Any]]:
    """Son window_min içindeki sistem-sağlık sinyal-event'leri (type SIGNAL_TYPES VEYA
    source watchdog:%). type=incident HARİÇ (recursive-önleme). Hata → [] (fail-safe)."""
    placeholders = ",".join("?" * len(SIGNAL_TYPES))
    sql = (
        "SELECT id, timestamp, type, source, severity, title FROM events "
        "WHERE timestamp > datetime('now', ?) "
        f"AND (type IN ({placeholders}) OR source LIKE 'watchdog:%') "
        "AND type != 'incident' ORDER BY timestamp"
    )
    try:
        con = get_conn(server_db_path(), readonly=True)
        try:
            rows = con.execute(sql, (f"-{int(window_min)} minutes", *SIGNAL_TYPES)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        logger.exception("correlation _read_signal_events sorgu hatası (fail-safe)")
        return []
    return [dict(r) for r in rows]


def correlate(events: list[dict[str, Any]], *, min_sources: int = CORRELATION_MIN_SOURCES) -> dict[str, Any] | None:
    """Pencere-içi sinyaller → tek incident dict (>= min_sources FARKLI kaynak varsa). Yoksa None.

    Fingerprint = sıralı-distinct-source'lar hash'i → AYNI kaynak-kümesi = AYNI incident (dedup).
    Tek-kaynak (yalın tekrar) incident DEĞİL — korelasyon cross-source ister."""
    if not events:
        return None
    sources = sorted({str(e["source"]) for e in events})
    if len(sources) < min_sources:
        return None
    fp = hashlib.sha1("|".join(sources).encode()).hexdigest()[:12]  # noqa: S324 — fingerprint (güvenlik değil)
    types = sorted({str(e["type"]) for e in events})
    return {
        "fingerprint": fp,
        "sources": sources,
        "types": types,
        "event_count": len(events),
        "detail": f"{len(sources)} ilişkili sinyal son {CORRELATION_WINDOW_MIN}dk: " + ", ".join(sources[:6]),
    }


def run_correlation_check(
    window_min: int = CORRELATION_WINDOW_MIN,
    min_sources: int = CORRELATION_MIN_SOURCES,
    events: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Tek tur: events-spine → cross-source korelasyon → emit_throttled(type=incident, warn).
    Fail-safe. `events` verilirse DB-okuma atlanır (test-injection). Döndürür: {signals, incident, emitted, suppressed}."""
    summary: dict[str, int] = {"signals": 0, "incident": 0, "emitted": 0, "suppressed": 0}
    try:
        if not _enabled():
            return summary
        evs = events if events is not None else _read_signal_events(window_min)
        summary["signals"] = len(evs)
        inc = correlate(evs, min_sources=min_sources)
        if inc is None:
            return summary
        summary["incident"] = 1
        res = emit_throttled(
            type="incident",
            source=f"incident:{inc['fingerprint']}",
            title=f"korele incident: {len(inc['sources'])} ilişkili sinyal ({'+'.join(inc['types'])})",
            severity="warn",
            detail=str(inc["detail"]),
            payload=inc,
            window_seconds=CORRELATION_DEDUP_SECONDS,
        )
        if res.emitted:
            summary["emitted"] += 1
        elif res.suppressed:
            summary["suppressed"] += 1
    except Exception:
        logger.exception("correlation-check hatası (fail-safe)")
    return summary
