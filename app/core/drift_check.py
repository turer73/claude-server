"""Gap-8 ingestion-producer: deployed≠running / config drift → events-spine.

İki drift-sinyali, mevcut altyapıyı genelleştirir (awareness-research gap-8, düşük efor):

1. SHA-drift (deployed≠running): `/health` HTTP-prob → `stale==True` ise çalışan-kod ≠ disk-HEAD
   (restart gerekli). NEDEN HTTP-prob (import DEĞİL): cron AYRI process; çalışan-server'ın
   startup-pinned `_DEPLOYED_SHA`'sını import'la bilemez (kendi import-anı SHA'sını alır).
   `/health` çalışan-gerçeği döndürür (sha=çalışan, disk_sha=disk, stale=fark).

2. config-drift: `dead_gate.audit_runtime_dead_gates` → `.env`'de var ama process-env'de YOK
   + os.environ.get-reader olan gate'ler (= sessiz no-op; #174 sınıfı). dead-gate guard'ın
   runtime-genişlemesi.

emit_throttled (gap-2 helper, 2. gerçek tüketici): persistent-drift (restart/fix edilene dek)
her cron-turunda RE-EMIT etmesin → WINDOW içinde bastır, periyodik re-surface. severity=warn
(warn DA Telegram-page'liyor = awareness doğru, gap-2 #100139 dersi).

NOT (kapsam): DB-schema vs migration drift MVP-DIŞI — proje migration-versiyon-sistemi yok
(CREATE TABLE IF NOT EXISTS → tablo hep var); düşük-değer, follow-up.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from app.core.config import DEFAULT_ENV_FILE, read_env_var
from app.core.emit_throttle import emit_throttled

logger = logging.getLogger(__name__)

HEALTH_URL = "http://localhost:8420/health"
DRIFT_WINDOW_SECONDS = 1800.0  # 30dk: persistent drift periyodik re-surface (restart/fix edilene dek)
DEFAULT_SOURCE_ROOTS: tuple[str, ...] = ("app",)


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


def config_drift(env_file: str = DEFAULT_ENV_FILE, source_roots: Iterable[str | Path] = DEFAULT_SOURCE_ROOTS) -> list[dict[str, Any]]:
    """dead_gate.audit_runtime_dead_gates → config-effect dead-gate'ler (`.env`'de var,
    process-env'de yok, os.environ.get-reader var = sessiz no-op). Hata → [] (fail-safe)."""
    try:
        from app.core.dead_gate import audit_runtime_dead_gates

        return [
            {
                "kind": "config",
                "gate": dg.name,
                "reader": dg.reader,
                "detail": f"dead-gate: {dg.name} .env'de var ama process-env'de yok ({dg.reader}) — silent no-op",
            }
            for dg in audit_runtime_dead_gates(env_file, source_roots)
        ]
    except Exception:
        logger.exception("config_drift dead_gate audit hatası (fail-safe)")
        return []


def run_drift_check(
    env_file: str = DEFAULT_ENV_FILE,
    source_roots: Iterable[str | Path] = DEFAULT_SOURCE_ROOTS,
    health_url: str = HEALTH_URL,
) -> dict[str, int]:
    """Tek tur: SHA-drift (/health stale) + config-drift (dead-gate) → emit_throttled(type=drift, warn).

    Fail-safe (cron'u bozmaz). emit_throttled → aynı (type, source) WINDOW içinde re-emit
    edilmez (persistent-drift cron-flood bastır). Döndürür: {sha_drift, config_drift, emitted, suppressed}."""
    summary: dict[str, int] = {"sha_drift": 0, "config_drift": 0, "emitted": 0, "suppressed": 0}
    try:
        if not _enabled():
            return summary
        drifts: list[dict[str, Any]] = []
        sd = sha_drift(health_url)
        if sd is not None:
            summary["sha_drift"] = 1
            drifts.append(sd)
        cds = config_drift(env_file, source_roots)
        summary["config_drift"] = len(cds)
        drifts.extend(cds)
        for d in drifts:
            source = "drift:sha" if d["kind"] == "sha" else f"drift:config:{d['gate']}"
            title = "deploy-drift: çalışan≠disk (restart gerekli)" if d["kind"] == "sha" else f"config-drift: dead-gate {d['gate']}"
            res = emit_throttled(
                type="drift",
                source=source,
                title=title,
                severity="warn",
                detail=str(d["detail"]),
                payload=d,
                window_seconds=DRIFT_WINDOW_SECONDS,
            )
            if res.emitted:
                summary["emitted"] += 1
            elif res.suppressed:
                summary["suppressed"] += 1
    except Exception:
        logger.exception("drift-check hatası (fail-safe)")
    return summary
