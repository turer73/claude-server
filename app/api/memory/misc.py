"""Device-projects + webhooks + search router handler'ları (memory paketi).

Küçük, ilişkili 3 alan tek modülde. Gövdeler birebir taşındı (Faz 3).
"""

from fastapi import Query

from app.api.memory import (
    _TELEGRAM_BOT_TOKEN,
    _TELEGRAM_CHAT_ID,
    DeviceProjectCreate,
    _ensure_webhooks_table,
    _send_telegram,
    get_db,
    router,
)

# ============ Device Projects ============


@router.get("/device-projects")
async def list_device_projects(device: str | None = None):
    db = get_db()
    try:
        query = "SELECT * FROM device_projects"
        params = []
        if device:
            query += " WHERE device_name=?"
            params.append(device)
        query += " ORDER BY last_activity DESC"
        return [dict(r) for r in db.execute(query, params).fetchall()]
    finally:
        db.close()


@router.post("/device-projects")
async def register_device_project(data: DeviceProjectCreate):
    db = get_db()
    try:
        db.execute(
            """
            INSERT INTO device_projects (device_name, project, local_path)
            VALUES (?, ?, ?)
            ON CONFLICT(device_name, project) DO UPDATE SET
                local_path=excluded.local_path, last_activity=datetime('now')
        """,
            (data.device_name, data.project, data.local_path),
        )
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()


# ============ Webhooks ============


@router.get("/webhooks")
async def list_webhooks():
    db = get_db()
    try:
        _ensure_webhooks_table(db)
        return [dict(r) for r in db.execute("SELECT * FROM webhooks ORDER BY event, id").fetchall()]
    finally:
        db.close()


@router.post("/webhooks")
async def register_webhook(event: str, url: str, secret: str = ""):
    db = get_db()
    try:
        _ensure_webhooks_table(db)
        existing = db.execute("SELECT id FROM webhooks WHERE event=? AND url=?", (event, url)).fetchone()
        if existing:
            return {"id": existing[0], "status": "already_exists"}
        cur = db.execute("INSERT INTO webhooks (event, url, secret) VALUES (?, ?, ?)", (event, url, secret))
        db.commit()
        return {"id": cur.lastrowid, "status": "created"}
    finally:
        db.close()


@router.delete("/webhooks/{webhook_id}")
async def delete_webhook(webhook_id: int):
    db = get_db()
    try:
        _ensure_webhooks_table(db)
        db.execute("DELETE FROM webhooks WHERE id=?", (webhook_id,))
        db.commit()
        return {"status": "deleted"}
    finally:
        db.close()


@router.get("/webhooks/telegram-status")
async def telegram_status():
    if not _TELEGRAM_BOT_TOKEN:
        return {"configured": False, "message": "TELEGRAM_BOT_TOKEN env eksik"}
    if not _TELEGRAM_CHAT_ID:
        return {"configured": False, "message": "TELEGRAM_CHAT_ID env eksik"}
    await _send_telegram("✅ <b>Klipper Hafıza Sistemi</b>\nTelegram bildirimleri aktif!")
    return {"configured": True, "message": "Test mesajı gönderildi"}


# ============ Search (FTS) ============


@router.get("/search")
async def search_all(q: str = Query(..., min_length=2)):
    """FTS + LIKE hibrit arama"""
    db = get_db()
    try:
        results = {}

        # FTS arama (discoveries)
        try:
            fts_rows = db.execute(
                "SELECT rowid, highlight(discoveries_fts, 0, '**', '**') as title, "
                "highlight(discoveries_fts, 1, '**', '**') as details "
                "FROM discoveries_fts WHERE discoveries_fts MATCH ? LIMIT 15",
                (q,),
            ).fetchall()
            fts_ids = [r[0] for r in fts_rows]
            if fts_ids:
                placeholders = ",".join("?" * len(fts_ids))
                results["discoveries"] = [
                    dict(r)
                    for r in db.execute(
                        f"SELECT id, project, type, title, status, device_name, date(created_at) as date "
                        f"FROM discoveries WHERE id IN ({placeholders})",
                        fts_ids,
                    ).fetchall()
                ]
            else:
                results["discoveries"] = []
        except Exception:
            # FTS fallback → LIKE
            pattern = f"%{q}%"
            results["discoveries"] = [
                dict(r)
                for r in db.execute(
                    "SELECT id, project, type, title, status, device_name FROM discoveries WHERE title LIKE ? OR details LIKE ? LIMIT 15",
                    (pattern, pattern),
                ).fetchall()
            ]

        # Memories — LIKE
        pattern = f"%{q}%"
        results["memories"] = [
            dict(r)
            for r in db.execute(
                "SELECT id, type, name, description FROM memories WHERE active=1 AND (content LIKE ? OR name LIKE ?) LIMIT 10",
                (pattern, pattern),
            ).fetchall()
        ]

        # Sessions — LIKE
        results["sessions"] = [
            dict(r)
            for r in db.execute(
                "SELECT id, session_num, date, device_name, substr(summary,1,100) as summary FROM sessions "
                "WHERE summary LIKE ? OR tasks_completed LIKE ? LIMIT 10",
                (pattern, pattern),
            ).fetchall()
        ]

        # Tasks — LIKE
        results["tasks"] = [
            dict(r)
            for r in db.execute(
                "SELECT id, project, task, device_name FROM tasks_log WHERE task LIKE ? OR details LIKE ? LIMIT 10", (pattern, pattern)
            ).fetchall()
        ]

        total = sum(len(v) for v in results.values())
        return {"query": q, "total": total, "results": results}
    finally:
        db.close()
