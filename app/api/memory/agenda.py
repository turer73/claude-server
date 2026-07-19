"""Gundem Panosu - tek-bakis agenda read-model endpoint (topic-3/P-D)."""

import asyncio
import sqlite3
from typing import Any

from app.api.memory import _ensure_status, _ensure_thread_fields, get_db, router
from app.api.memory import signal_quality as sq
from app.api.memory.claims import _ensure_claims, _expire_stale


@router.get("/agenda")
async def get_agenda() -> dict[str, Any]:
    return await asyncio.to_thread(_agenda_query)


def _agenda_query(device: str | None = None) -> dict[str, Any]:
    db = get_db()
    try:
        # discoveries.importance / notes.msg_type+status / active_claims lazy-migrated
        # onkosullar (discoveries.py/claims.py'deki ensure cagrilariyla ayni sozlesme).
        sq.ensure_signal_columns(db)
        _ensure_thread_fields(db)
        _ensure_status(db)
        _ensure_claims(db)
        s: dict[str, Any] = {}
        if device:
            notes_sql = (
                "SELECT id,from_device,title,substr(content,1,150) as content,msg_type,date(created_at) as date "
                "FROM notes WHERE COALESCE(status,'active')='active' AND (to_device=? OR to_device IS NULL) "
                "ORDER BY created_at DESC LIMIT 5"
            )
            notes_rows = db.execute(notes_sql, (device,)).fetchall()
        else:
            notes_sql = (
                "SELECT id,from_device,title,substr(content,1,150) as content,msg_type,date(created_at) as date "
                "FROM notes WHERE COALESCE(status,'active')='active' "
                "ORDER BY created_at DESC LIMIT 5"
            )
            notes_rows = db.execute(notes_sql).fetchall()
        s["ne_oldu"] = {
            "discoveries": [
                dict(r)
                for r in db.execute(
                    "SELECT id,project,type,title,status,device_name,importance,date(created_at) as date "
                    "FROM discoveries WHERE created_at > datetime('now','-48 hours') "
                    "ORDER BY COALESCE(importance,5) DESC,created_at DESC LIMIT 15"
                ).fetchall()
            ],
            "tasks": [
                dict(r)
                for r in db.execute(
                    "SELECT id,project,task,status,device_name,date(created_at) as date "
                    "FROM tasks_log WHERE created_at > datetime('now','-48 hours') "
                    "ORDER BY created_at DESC LIMIT 10"
                ).fetchall()
            ],
            "sessions": [
                dict(r)
                for r in db.execute(
                    "SELECT id,session_num,date,device_name,substr(summary,1,100) as summary FROM sessions ORDER BY id DESC LIMIT 5"
                ).fetchall()
            ],
            "notes": [dict(r) for r in notes_rows],
        }
        s["yapilacaklar"] = {
            "active_bugs": [
                dict(r)
                for r in db.execute(
                    "SELECT id,project,type,title,device_name,importance,date(created_at) as date "
                    "FROM discoveries WHERE type='bug' AND status='active' "
                    "ORDER BY COALESCE(importance,5) DESC,created_at DESC LIMIT 10"
                ).fetchall()
            ],
            "open_discoveries": [
                dict(r)
                for r in db.execute(
                    "SELECT id,project,type,title,device_name,importance,date(created_at) as date "
                    "FROM discoveries WHERE status='active' AND type!='bug' "
                    "ORDER BY COALESCE(importance,5) DESC,created_at DESC LIMIT 15"
                ).fetchall()
            ],
            "pending_tasks": [
                dict(r)
                for r in db.execute(
                    "SELECT id,project,task,status,device_name,date(created_at) as date "
                    "FROM tasks_log WHERE status IN ('pending','in_progress') "
                    "ORDER BY created_at DESC LIMIT 10"
                ).fetchall()
            ],
            "open_claims": _safe_claims(db),
        }
        s["kontrol_edilecekler"] = {
            "never_read_important": [
                dict(r)
                for r in db.execute(
                    "SELECT id,project,type,title,importance,date(created_at) as date "
                    "FROM discoveries WHERE read_count=0 AND importance>=7 AND status='active' "
                    "ORDER BY importance DESC LIMIT 10"
                ).fetchall()
            ],
            "stale_active_30d": [
                dict(r)
                for r in db.execute(
                    "SELECT id,project,type,title,status,device_name,date(created_at) as date,"
                    "CAST(julianday('now')-julianday(created_at) AS INTEGER) as days_old "
                    "FROM discoveries WHERE status='active' AND created_at < datetime('now','-30 days') "
                    "ORDER BY days_old DESC LIMIT 10"
                ).fetchall()
            ],
            "total_never_read": db.execute("SELECT COUNT(*) FROM discoveries WHERE read_count=0 AND status='active'").fetchone()[0],
        }
        dl = _device_health(db)
        s["ajan_saglik"] = {
            "devices": dl,
            "silent_devices": [d for d in dl if d["silent"]],
            "active_device_count": sum(1 for d in dl if not d["silent"]),
        }
        return s
    finally:
        db.close()


def _safe_claims(db: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        _expire_stale(db)
        db.commit()
        return [
            dict(r)
            for r in db.execute(
                "SELECT id,task_key,device,repo,branch,note,datetime(created_at) as created_at "
                "FROM active_claims WHERE active=1 AND expires_at >= datetime('now') "
                "ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
        ]
    except sqlite3.OperationalError as exc:
        if "no such table: active_claims" in str(exc):
            return []
        raise


def _device_health(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Use the freshest registered or observed activity for every device identity."""
    return [
        dict(r)
        for r in db.execute(
            """
            WITH activity(name, activity_jd) AS (
                SELECT device_name, MAX(julianday(created_at))
                FROM sessions WHERE NULLIF(device_name, '') IS NOT NULL GROUP BY device_name
                UNION ALL
                SELECT from_device, MAX(julianday(created_at))
                FROM notes WHERE NULLIF(from_device, '') IS NOT NULL GROUP BY from_device
                UNION ALL
                SELECT device_name, MAX(julianday(created_at))
                FROM tasks_log WHERE NULLIF(device_name, '') IS NOT NULL GROUP BY device_name
                UNION ALL
                SELECT device_name, MAX(julianday(created_at))
                FROM discoveries WHERE NULLIF(device_name, '') IS NOT NULL GROUP BY device_name
            ),
            freshest(name, activity_jd) AS (
                SELECT name, MAX(activity_jd) FROM activity GROUP BY name
            ),
            all_devices(name, platform, registered_jd) AS (
                SELECT name, platform, julianday(last_seen) FROM devices
                UNION ALL
                SELECT f.name, '?', NULL
                FROM freshest f LEFT JOIN devices d ON d.name=f.name
                WHERE d.name IS NULL
            ),
            resolved(name, platform, last_jd) AS (
                SELECT d.name, d.platform,
                       CASE
                           WHEN d.registered_jd IS NULL OR f.activity_jd > d.registered_jd THEN f.activity_jd
                           ELSE d.registered_jd
                       END
                FROM all_devices d LEFT JOIN freshest f ON f.name=d.name
            )
            SELECT name, platform, datetime(last_jd) AS last_seen,
                   CASE
                       WHEN last_jd IS NULL OR last_jd < julianday('now','-1 day') THEN 1
                       ELSE 0
                   END AS silent
            FROM resolved ORDER BY name
            """
        ).fetchall()
    ]
