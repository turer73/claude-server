"""Gap-8 ingestion-producer: deployed≠running (SHA) drift → events-spine.

SHA-drift (deployed≠running): `/health` HTTP-prob → `stale==True` ise çalışan-kod ≠ disk-HEAD
(restart gerekli). NEDEN HTTP-prob (import DEĞİL): cron AYRI process; çalışan-server'ın
startup-pinned `_DEPLOYED_SHA`'sını import'la bilemez (kendi import-anı SHA'sını alır).
`/health` çalışan-gerçeği döndürür (sha=çalışan, disk_sha=disk, stale=fark).

emit_throttled (gap-2 helper): persistent-drift (restart edilene dek) her cron-turunda
RE-EMIT etmesin → WINDOW içinde bastır, periyodik re-surface. severity=warn (gap-2 #100139).

CONFIG-DRIFT NEDEN BURADA YOK (Codex #196 fix): dead-gate config-drift `audit_runtime_dead_gates`
ile YAPILIYOR ama (a) cron-wrap `.env`'i os.environ'a yüklediği için CRON-context'te no-op
(`.env`-key not-in-os.environ → boş) + (b) ZATEN main.py boot-audit'inde (startup, DOĞRU
service-env'de) WARN-log + discovery-emit ile yapılıyor → cron-tarafı hem bozuk hem redundant.
Kaldırıldı; runtime-dead-gate tespiti boot-audit'in sorumluluğu.

NOT (kapsam): DB-schema vs migration drift MVP-DIŞI — proje migration-versiyon-sistemi yok.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from app.core.config import read_env_var
from app.core.emit_throttle import emit_throttled

logger = logging.getLogger(__name__)

HEALTH_URL = "http://localhost:8420/health"
DRIFT_WINDOW_SECONDS = 1800.0  # 30dk: persistent drift periyodik re-surface (restart edilene dek)


def _enabled() -> bool:
    """Kill-switch (default ON). read_env_var (#174 sınıfı; early-return'de kullanılır → dead_gate-temiz)."""
    return (read_env_var("DRIFT_CHECK_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")


def sha_drift(health_url: str = HEALTH_URL, timeout: float = 5.0) -> dict[str, Any] | None:
    """GET /health → `stale==True` ise deployed≠running drift dict, aksi/None.

    stale=None (SHA-belirlenemez) veya server-down/unreachable → None (drift İDDİA ETME;
    liveness ayrı mesele). Yalnız stale=True kesin-drift."""
    try:
        req = urllib.request.Request(health_url, headers={"User-Agent": "klipper-drift/1"})  # noqa: S310
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    if data.get("stale") is True:
        running = str(data.get("sha") or "?")[:8]
        disk = str(data.get("disk_sha") or "?")[:8]
        return {
            "kind": "sha",
            "running_sha": data.get("sha"),
            "disk_sha": data.get("disk_sha"),
            "detail": f"deployed≠running: çalışan {running} ≠ disk {disk} (restart gerekli)",
        }
    return None


def run_drift_check(health_url: str = HEALTH_URL) -> dict[str, int]:
    """Tek tur: SHA-drift (/health stale) → emit_throttled(type=drift, warn).

    Fail-safe (cron'u bozmaz). emit_throttled → aynı (type, source) WINDOW içinde re-emit
    edilmez (persistent-drift cron-flood bastır). Döndürür: {sha_drift, emitted, suppressed}."""
    summary: dict[str, int] = {"sha_drift": 0, "emitted": 0, "suppressed": 0}
    try:
        if not _enabled():
            return summary
        sd = sha_drift(health_url)
        if sd is None:
            return summary
        summary["sha_drift"] = 1
        res = emit_throttled(
            type="drift",
            source="drift:sha",
            title="deploy-drift: çalışan≠disk (restart gerekli)",
            severity="warn",
            detail=str(sd["detail"]),
            payload=sd,
            window_seconds=DRIFT_WINDOW_SECONDS,
        )
        if res.emitted:
            summary["emitted"] += 1
        elif res.suppressed:
            summary["suppressed"] += 1
    except Exception:
        logger.exception("drift-check hatası (fail-safe)")
    return summary
