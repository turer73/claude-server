"""Dashboard router handler (memory paketi). Gövde birebir taşındı (Faz 3)."""

import asyncio

from app.api.memory import get_db, router


def _dashboard_query():
    """Akıllı dashboard — stale detection, proje health, action items"""
    db = get_db()
    try:
        stats = {
            "memories": db.execute("SELECT COUNT(*) FROM memories WHERE active=1").fetchone()[0],
            "sessions": db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            "tasks": db.execute("SELECT COUNT(*) FROM tasks_log").fetchone()[0],
            "discoveries": db.execute("SELECT COUNT(*) FROM discoveries").fetchone()[0],
            "open_bugs": db.execute("SELECT COUNT(*) FROM discoveries WHERE type='bug' AND status='active'").fetchone()[0],
            "architecture": db.execute("SELECT COUNT(*) FROM discoveries WHERE type='architecture' AND status='active'").fetchone()[0],
            "active_plans": db.execute("SELECT COUNT(*) FROM discoveries WHERE type='plan' AND status='active'").fetchone()[0],
            "completed_plans": db.execute("SELECT COUNT(*) FROM discoveries WHERE type='plan' AND status='completed'").fetchone()[0],
            "fixes": db.execute("SELECT COUNT(*) FROM discoveries WHERE type='fix'").fetchone()[0],
            "unread_notes": db.execute("SELECT COUNT(*) FROM notes WHERE read=0").fetchone()[0],
        }

        devices = [
            dict(r)
            for r in db.execute("SELECT name, platform, hostname, tailscale_ip, last_seen FROM devices ORDER BY last_seen DESC").fetchall()
        ]

        recent_sessions = [
            dict(r)
            for r in db.execute(
                "SELECT session_num, date, device_name, platform, substr(summary,1,100) as summary FROM sessions ORDER BY id DESC LIMIT 5"
            ).fetchall()
        ]

        open_bugs = [
            dict(r)
            for r in db.execute(
                "SELECT id, project, title, device_name, created_at FROM discoveries "
                "WHERE type='bug' AND status='active' ORDER BY created_at DESC"
            ).fetchall()
        ]

        # Stale data — 60+ gün okunamayan active kayıtlar
        stale = [
            dict(r)
            for r in db.execute(
                "SELECT id, project, type, title, date(created_at) as created, read_count "
                "FROM discoveries WHERE status='active' AND read_count=0 "
                "AND created_at < datetime('now', '-60 days') ORDER BY created_at LIMIT 10"
            ).fetchall()
        ]

        # Hiç okunmamış kayıt sayısı
        never_read = db.execute("SELECT COUNT(*) FROM discoveries WHERE read_count=0").fetchone()[0]

        # Proje bazlı özet
        projects = [
            dict(r)
            for r in db.execute(
                "SELECT project, COUNT(*) as total, "
                "SUM(CASE WHEN type='bug' AND status='active' THEN 1 ELSE 0 END) as open_bugs, "
                "SUM(CASE WHEN type='architecture' THEN 1 ELSE 0 END) as arch, "
                "SUM(CASE WHEN type='plan' AND status='active' THEN 1 ELSE 0 END) as active_plans "
                "FROM discoveries GROUP BY project ORDER BY total DESC"
            ).fetchall()
        ]

        return {
            "stats": stats,
            "devices": devices,
            "recent_sessions": recent_sessions,
            "open_bugs": open_bugs,
            "stale_data": stale,
            "never_read_count": never_read,
            "projects": projects,
        }
    finally:
        db.close()


@router.get("/dashboard")
async def memory_dashboard():
    """Akıllı dashboard — Faz 2: sync DB to_thread'e offload (event-loop blokmaz)."""
    return await asyncio.to_thread(_dashboard_query)
