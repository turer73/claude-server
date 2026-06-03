"""LIVESYS Faz 3.2 — hafif olay omurgası (event backbone).

Dağınık olay-üreticileri (cron_outcomes, liveness, pr-review, alerts, deploy/fix)
TEK merkezi `server.db.events` kaydına route eder. Deterministik kayıt + eşik —
Claude'u kalp-atışı yapmaz. severity>=warn olaylar bildirilir (idempotent: notified
flag). digest + alert okur.

emit_event(): üreticiler çağırır (Python). Bash üreticiler sqlite3-direct yazar
(cron_outcomes deseni). Pure-ish: events'e yazar, başka runtime'a dokunmaz.
"""

from __future__ import annotations

import json
import os
import sqlite3

from app.db.database import DEFAULT_DB_PATH

# Tek runtime gerçeği os.environ["DB_PATH"] (prod systemd set eder). Env yoksa
# main.py schema'yı DEFAULT_DB_PATH'e kurar -> emit/read AYNI sabiti kullanmalı,
# yoksa events farklı/tablosuz path'e yazıp sessizce drop olur (Codex #18 P2).
DB_PATH = DEFAULT_DB_PATH
SEVERITIES = ("info", "warn", "critical")
# Mevcut alert üreticileri (devops_agent.py, alert-check.sh) "warning"/"error"
# vocabulary'si kullanıyor. Bunları kanonik severity'ye eşle; aksi halde
# "warning" -> info'ya düşer ve pending_notifications (warn/critical) sessizce eler.
_SEVERITY_ALIAS = {"warning": "warn", "error": "critical", "err": "critical", "crit": "critical"}


def _db_path() -> str:
    return os.environ.get("DB_PATH") or DB_PATH


def _normalize_severity(severity: str | None) -> str:
    s = (severity or "info").strip().lower()
    s = _SEVERITY_ALIAS.get(s, s)
    return s if s in SEVERITIES else "info"


def _serialize_payload(payload: dict | None) -> str | None:
    """payload -> JSON string (best-effort). datetime/Path/bytes gibi JSON-native
    olmayan değerler emit_event'i ASLA crash etmemeli (modül 'hata→None' sözleşmesi,
    Claude-heartbeat değil). default=str çoğunu çözer; kalan (circular vb.) için repr."""
    if payload is None:
        return None
    try:
        return json.dumps(payload, default=str)
    except (TypeError, ValueError):
        return json.dumps({"_unserializable": repr(payload)[:500]})


def emit_event(
    type: str,
    source: str,
    title: str,
    severity: str = "info",
    detail: str | None = None,
    payload: dict | None = None,
) -> int | None:
    """Merkezi events tablosuna bir olay yaz. id döner (hata/geçersiz → None)."""
    severity = _normalize_severity(severity)
    if not type or not source or not title:
        return None
    try:
        con = sqlite3.connect(_db_path())
        try:
            cur = con.execute(
                "INSERT INTO events (type, source, severity, title, detail, payload) VALUES (?,?,?,?,?,?)",
                (type, source, severity, title, detail, _serialize_payload(payload)),
            )
            con.commit()
            return cur.lastrowid
        finally:
            con.close()
    except sqlite3.Error:
        return None


def _sev_at_least(min_severity: str) -> list[str]:
    if min_severity not in SEVERITIES:
        return list(SEVERITIES)
    return list(SEVERITIES[SEVERITIES.index(min_severity) :])


def recent_events(hours: int = 24, min_severity: str | None = None) -> list[dict]:
    """Son `hours` saatteki olaylar (min_severity ve üstü). Hata → []."""
    sevs = _sev_at_least(min_severity) if min_severity else list(SEVERITIES)
    placeholders = ",".join("?" * len(sevs))
    try:
        con = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                f"SELECT id, timestamp, type, source, severity, title, detail, notified "
                f"FROM events WHERE timestamp > datetime('now', ?) AND severity IN ({placeholders}) "
                f"ORDER BY id DESC LIMIT 50",
                (f"-{int(hours)} hours", *sevs),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def pending_notifications() -> list[dict]:
    """notified=0 + severity>=warn olaylar (bildirilecekler). Hata → []."""
    try:
        con = sqlite3.connect(f"file:{_db_path()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT id, timestamp, type, source, severity, title, detail FROM events "
                "WHERE notified=0 AND severity IN ('warn','critical') ORDER BY id LIMIT 50"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def mark_notified(ids: list[int]) -> int:
    """Verilen event id'lerini notified=1 yap. Etkilenen satır sayısı döner."""
    if not ids:
        return 0
    try:
        con = sqlite3.connect(_db_path())
        try:
            cur = con.executemany("UPDATE events SET notified=1 WHERE id=?", [(i,) for i in ids])
            con.commit()
            return cur.rowcount
        finally:
            con.close()
    except sqlite3.Error:
        return 0
