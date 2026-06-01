"""Admin API — server-side .env secrets management (dashboard).

Auth: JWT (require_auth from middleware) — dashboard kullanir.
Memory router'in X-Memory-Key auth'unu kirmaz; secrets endpoint'leri
dashboard ile uyumlu olacak sekilde ayri router'a yerlestirildi.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator

from app.middleware.dependencies import require_auth

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

DB_PATH = "/opt/linux-ai-server/data/claude_memory.db"


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# P1.1: memory.name prefix -> (event_type, label, severity)
# Sira KRITIK: uzun-prefix-once (autonomous-spawn-poison- vs autonomous-spawn-)
_PREFIX_MAP: list[tuple[str, str, str | None, str]] = [
    ("autonomous-spawn-poison-",     "dlq",           None,         "error"),
    ("autonomous-audit-suspicious-", "audit",         None,         "warn"),
    ("autonomous-threat-detect-",    "threat",        None,         "critical"),
    ("autonomous-health-fail-",      "health",        None,         "warn"),
    ("autonomous-lock-cleanup-",     "lock_cleanup",  None,         "info"),
    ("autonomous-daily-summary-",    "daily_summary", None,         "info"),
    ("autonomous-ack-",              "classify",      "ACK",        "ok"),
    ("autonomous-deferred-",         "classify",      "DISCUSSION", "info"),
    ("autonomous-urgent-",           "urgent",        "URGENT",     "critical"),
    ("autonomous-spawn-",            "spawn",         "ACTIONABLE", "ok"),
]

_NOTE_ID_RE = re.compile(r"-(\d+)(?:-|$)")

# Note-id'si olmayan event tipleri — _extract_note_id butun -DDDD- match'lerini
# yakalar; bu tiplerde tarih kismi yanlislikla note_id zannedilir.
# 'memory' fallback de note_id'siz (bilinmeyen autonomous-* slug, ornek
# autonomous-research-* tarih yakalanmasin).
_NO_NOTE_ID_TYPES = {"health", "lock_cleanup", "daily_summary", "memory"}


def _classify_memory(name: str) -> tuple[str, str | None, str]:
    """Memory.name prefix lookup. Returns (event_type, label, severity)."""
    for prefix, etype, label, severity in _PREFIX_MAP:
        if name.startswith(prefix):
            return etype, label, severity
    return "memory", None, "info"


def _extract_note_id(name: str, etype: str) -> int | None:
    """autonomous-spawn-173-20260518 -> 173. Tarih-only event'lerde None."""
    if etype in _NO_NOTE_ID_TYPES:
        return None
    m = _NOTE_ID_RE.search(name)
    return int(m.group(1)) if m else None


class SecretSet(BaseModel):
    key: str
    value: str

    @field_validator("key")
    @classmethod
    def validate_key(cls, v: str) -> str:
        if not re.match(r"^[A-Z_][A-Z0-9_]*$", v):
            raise ValueError("key must match ^[A-Z_][A-Z0-9_]*$")
        if len(v) > 80:
            raise ValueError("key too long (>80)")
        return v

    @field_validator("value")
    @classmethod
    def validate_value(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("value cannot be empty")
        if len(v) > 4000:
            raise ValueError("value too long (>4000)")
        return v


ENV_PATH = "/opt/linux-ai-server/.env"
HELPER_PATH = "/opt/linux-ai-server/scripts/set-env-secret.sh"


@router.get("/secrets")
async def list_secrets(_: None = Depends(require_auth)) -> dict:
    """.env'deki KEY listesi. Value asla donmez — sadece key + length."""
    if not os.path.exists(ENV_PATH):
        return {"count": 0, "keys": []}
    keys = []
    try:
        with open(ENV_PATH) as f:
            for line in f:
                line = line.rstrip("\n\r")
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if not re.match(r"^[A-Z_][A-Z0-9_]*$", k):
                    continue
                keys.append({"key": k, "length": len(v)})
    except OSError as e:
        raise HTTPException(500, f"read failed: {e}")
    return {"count": len(keys), "keys": sorted(keys, key=lambda r: r["key"])}


@router.post("/secrets")
async def set_secret(data: SecretSet, _: None = Depends(require_auth)) -> dict:
    """.env'e KEY=VALUE upsert. Helper subprocess (idempotent)."""
    if not os.path.exists(HELPER_PATH):
        raise HTTPException(500, f"helper missing: {HELPER_PATH}")
    try:
        proc = subprocess.run(
            ["bash", HELPER_PATH, data.key],
            input=data.value,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(500, "helper timeout")
    if proc.returncode != 0:
        raise HTTPException(400, f"helper rc={proc.returncode}: {proc.stderr.strip()[:200]}")
    return {
        "key": data.key,
        "action": proc.stdout.strip(),
        "value_length": len(data.value),
    }


# ============ Autonomous flow visualization (P1.1) ============

@router.get("/autonomous/timeline")
async def autonomous_timeline(
    limit: int = Query(50, ge=1, le=200),
    _: None = Depends(require_auth),
) -> dict:
    """
    Otonom akis timeline: memories (autonomous-*) + spawn_failures + new_notes
    in-memory merge, DESC sort, son `limit` event.
    """
    db = _get_db()
    try:
        # 1. Memories — autonomous-* prefix'li
        mem_rows = db.execute(
            "SELECT id, name, description, created_at FROM memories "
            "WHERE name LIKE 'autonomous-%' AND active=1 "
            "ORDER BY created_at DESC LIMIT 200"
        ).fetchall()

        # 2. Spawn failures (archived hariç; archived spawn-* memory'sinden zaten gozukur)
        sf_rows = db.execute(
            "SELECT id, note_id, from_device, title, attempt_num, exit_code, "
            "status, first_failed_at, last_retry_at, poisoned_at "
            "FROM spawn_failures WHERE status != 'archived' "
            "ORDER BY first_failed_at DESC LIMIT 100"
        ).fetchall()

        # 3. Notes — son N, in-memory'de unclassified ayikla
        note_rows = db.execute(
            "SELECT id, from_device, title, created_at, read "
            "FROM notes ORDER BY id DESC LIMIT 100"
        ).fetchall()
    finally:
        db.close()

    events = []
    classified_note_ids: set[int] = set()

    # Memory events
    for r in mem_rows:
        etype, label, severity = _classify_memory(r["name"])
        nid = _extract_note_id(r["name"], etype)
        if nid is not None:
            classified_note_ids.add(nid)
        events.append({
            "at": r["created_at"],
            "type": etype,
            "note_id": nid,
            "label": label,
            "title": (r["description"] or "")[:120],
            "details": "",
            "severity": severity,
            "source": f"memory#{r['id']}",
        })

    # Spawn-failure events (DLQ)
    for r in sf_rows:
        events.append({
            "at": r["last_retry_at"] or r["first_failed_at"],
            "type": "dlq",
            "note_id": r["note_id"],
            "label": r["status"],
            "title": (r["title"] or "")[:120],
            "details": f"attempt={r['attempt_num']} rc={r['exit_code']}",
            "severity": "error" if r["status"] == "poison" else "warn",
            "source": f"spawn_failures#{r['id']}",
        })

    # New-note events (autonomous-* entry'si olmayan)
    for r in note_rows:
        if r["id"] in classified_note_ids:
            continue
        events.append({
            "at": r["created_at"],
            "type": "new_note",
            "note_id": r["id"],
            "label": None,
            "title": (r["title"] or "")[:120],
            "details": f"from={r['from_device']} (siniflandirma bekleniyor)" if not r["read"] else f"from={r['from_device']} (okundu)",
            "severity": "info",
            "source": f"notes#{r['id']}",
        })

    # DESC sort + limit
    events.sort(key=lambda e: e["at"] or "", reverse=True)
    events = events[:limit]

    return {"count": len(events), "events": events}


@router.get("/autonomous/stats")
async def autonomous_stats(
    days: int = Query(7, ge=1, le=90),
    _: None = Depends(require_auth),
) -> dict:
    """Otonom akis stats (son N gun): classification dagilim, DLQ counts, spawn_rate, alerts."""
    modifier = f"-{days} days"
    db = _get_db()
    try:
        # Classification counts (autonomous-spawn-poison- once filtreliyoruz)
        c = db.execute(
            "SELECT "
            "  SUM(CASE WHEN name LIKE 'autonomous-ack-%' THEN 1 ELSE 0 END) AS ack, "
            "  SUM(CASE WHEN name LIKE 'autonomous-spawn-%' "
            "AND name NOT LIKE 'autonomous-spawn-poison-%' THEN 1 ELSE 0 END) AS actionable, "
            "  SUM(CASE WHEN name LIKE 'autonomous-deferred-%' THEN 1 ELSE 0 END) AS discussion, "
            "  SUM(CASE WHEN name LIKE 'autonomous-urgent-%' THEN 1 ELSE 0 END) AS urgent "
            "FROM memories WHERE active=1 AND created_at >= datetime('now', ?)",
            (modifier,),
        ).fetchone()
        classification = {
            "ACK": int(c["ack"] or 0),
            "ACTIONABLE": int(c["actionable"] or 0),
            "DISCUSSION": int(c["discussion"] or 0),
            "URGENT": int(c["urgent"] or 0),
        }

        # DLQ status counts
        dlq = {"pending_retry": 0, "poison": 0, "archived": 0, "orphaned": 0}
        for r in db.execute(
            "SELECT status, COUNT(*) AS n FROM spawn_failures "
            "WHERE first_failed_at >= datetime('now', ?) GROUP BY status",
            (modifier,),
        ):
            if r["status"] in dlq:
                dlq[r["status"]] = int(r["n"])

        # Alerts
        a = db.execute(
            "SELECT "
            "  SUM(CASE WHEN name LIKE 'autonomous-audit-suspicious-%' THEN 1 ELSE 0 END) AS audit, "
            "  SUM(CASE WHEN name LIKE 'autonomous-threat-detect-%' THEN 1 ELSE 0 END) AS threat, "
            "  SUM(CASE WHEN name LIKE 'autonomous-health-fail-%' THEN 1 ELSE 0 END) AS health_fail "
            "FROM memories WHERE active=1 AND created_at >= datetime('now', ?)",
            (modifier,),
        ).fetchone()
        alerts = {
            "urgent": classification["URGENT"],
            "audit": int(a["audit"] or 0),
            "threat": int(a["threat"] or 0),
            "health_fail": int(a["health_fail"] or 0),
        }

        # Spawn rate: success = actionable success, fail = dlq active states
        success = classification["ACTIONABLE"]
        fail = dlq["pending_retry"] + dlq["poison"] + dlq["orphaned"]
        total = success + fail
        success_pct: float | None = round(success / total * 100, 1) if total > 0 else None

        # Unclassified notes (autonomous memory entry'si olmayan)
        # note: '%-{id}-%' false-positive (note 173 vs 1730) kabul edildi MVP'de
        unc = db.execute(
            "SELECT COUNT(*) AS n FROM notes n WHERE n.created_at >= datetime('now', ?) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM memories m WHERE m.active=1 AND m.name LIKE 'autonomous-%' "
            "  AND (m.name LIKE 'autonomous-%-' || n.id OR m.name LIKE 'autonomous-%-' || n.id || '-%')"
            ")",
            (modifier,),
        ).fetchone()
    finally:
        db.close()

    return {
        "period_days": days,
        "classification": classification,
        "dlq": dlq,
        "spawn_rate": {"success": success, "fail": fail, "success_pct": success_pct},
        "alerts": alerts,
        "unclassified_notes": int(unc["n"] or 0),
    }
