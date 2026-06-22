"""emit_throttled — kalıcı (DB-bazlı) dedup/throttle wrapper, emit_event üzerine.

Gap-2 (FastAPI exception-producer) ve agent-watchdog AYNI ihtiyaca sahip: aynı
(type, source) olayı kısa pencerede TEKRAR emit etme (flood-bastır, klipper #100128).
Bu helper o deseni TEK yerde toplar (DRY).

NEDEN DB-BAZLI (in-proc dict DEĞİL): agent-watchdog ayrı CRON-process'tir
(automation/agent-watchdog.py her */3'te yeni process). In-proc state runs-arası
PAYLAŞILMAZ → watchdog'u dedup edemez (her run boş dict). events-tablosu sorgusu
process-bağımsız → hem cron-watchdog hem uzun-yaşayan-server-exception için çalışır.
Yan-fayda: in-proc state YOK → "unbounded cooldown dict" riski hiç doğmaz.

THROTTLE: son aynı-(type, source) olayın yaşı < window → suppress (emit YOK). Hiç-yok
ya da yaş >= window → emit. novel = hiç-önceki-yok (ilk-kez). Tek SELECT ikisini de
verir (kalıcı-novelty zaten events'te; bu sadece pencere-içi flood-suppression).

FAIL-OPEN (#186 dersi — sessiz-suppress maskeler): stats-sorgusu hata verirse emit'e
düş (event kaybetmek, sahte-suppress'ten iyi) + logger.exception (görünür).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.core.events import emit_event
from app.db.data_layer import get_conn, server_db_path

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_SECONDS = 600.0  # 10 dk


@dataclass(frozen=True)
class ThrottleResult:
    """emit_throttled sonucu. Çağıran novel/suppressed'i farkındalık için kullanabilir."""

    emitted: bool
    event_id: int | None
    novel: bool  # ilk-kez mi (hiç önceki (type, source) yok)
    prior_age_seconds: float | None  # önceki aynı-olayın yaşı (sn); novel/hata → None
    suppressed: bool  # pencere-içi olduğu için emit edilmedi mi


def _prior_stats(type: str, source: str) -> tuple[float | None, int, bool]:
    """(en-son-aynı-olay-yaşı-sn | None, adet, sorgu-başarılı-mı) döner.

    julianday('now') ve julianday(timestamp) ikisi de UTC (events.timestamp =
    datetime('now') UTC) → yaş tutarlı. Hata → (None, 0, False): fail-open (emit).
    """
    try:
        con = get_conn(server_db_path(), readonly=True)
        try:
            row = con.execute(
                "SELECT (julianday('now') - julianday(MAX(timestamp))) * 86400.0 AS age_s, "
                "COUNT(*) AS cnt FROM events WHERE type=? AND source=?",
                (type, source),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        logger.exception("emit_throttled prior-stats sorgusu hata (fail-open: emit)")
        return (None, 0, False)
    if not row:
        return (None, 0, True)
    cnt = int(row["cnt"]) if row["cnt"] is not None else 0
    age = row["age_s"]
    return (float(age) if age is not None else None, cnt, True)


def emit_throttled(
    *,
    type: str,
    source: str,
    title: str,
    severity: str = "info",
    detail: str | None = None,
    payload: dict[str, Any] | None = None,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
) -> ThrottleResult:
    """Aynı (type, source) son `window_seconds` içinde emit edildiyse BASTIR; aksi
    halde emit_event çağır. payload'a `novel` + `throttle` meta eklenir."""
    age, cnt, ok = _prior_stats(type, source)
    # Pencere-içi tekrar → suppress (yalnız stats-sorgusu başarılıyken; hata → fail-open).
    if ok and cnt > 0 and age is not None and age < window_seconds:
        return ThrottleResult(emitted=False, event_id=None, novel=False, prior_age_seconds=age, suppressed=True)
    novel = bool(ok and cnt == 0)
    enriched = dict(payload or {})
    enriched["novel"] = novel
    enriched["throttle"] = {
        "window_s": window_seconds,
        "prior_age_s": round(age, 1) if age is not None else None,
    }
    eid = emit_event(type=type, source=source, title=title, severity=severity, detail=detail, payload=enriched)
    return ThrottleResult(
        emitted=eid is not None,
        event_id=eid,
        novel=novel,
        prior_age_seconds=age,
        suppressed=False,
    )
