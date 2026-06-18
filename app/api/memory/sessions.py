"""Session router handler'ları (memory paketi). Gövdeler birebir taşındı (Faz 3)."""

import asyncio
import json

from fastapi import HTTPException

from app.api.memory import SessionCreate, _fire_event, get_db, router


@router.get("/sessions")
async def list_sessions(device: str | None = None, platform: str | None = None, limit: int = 20):
    db = get_db()
    try:
        query = "SELECT id, session_num, date, device_name, platform, summary FROM sessions WHERE 1=1"
        params = []
        if device:
            query += " AND device_name=?"
            params.append(device)
        if platform:
            query += " AND platform=?"
            params.append(platform)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in db.execute(query, params).fetchall()]
    finally:
        db.close()


@router.get("/sessions/{session_id}")
async def get_session(session_id: int):
    db = get_db()
    try:
        session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            raise HTTPException(404, "Session not found")
        result = dict(session)
        result["tasks"] = [dict(r) for r in db.execute("SELECT * FROM tasks_log WHERE session_id=?", (session_id,)).fetchall()]
        result["discoveries"] = [dict(r) for r in db.execute("SELECT * FROM discoveries WHERE session_id=?", (session_id,)).fetchall()]
        return result
    finally:
        db.close()


@router.post("/sessions")
async def create_session(data: SessionCreate):
    db = get_db()
    try:
        if not data.session_num:
            row = db.execute("SELECT COALESCE(MAX(session_num),0)+1 FROM sessions WHERE device_name=?", (data.device_name,)).fetchone()
            data.session_num = row[0]

        device = db.execute("SELECT id, platform FROM devices WHERE name=?", (data.device_name,)).fetchone()
        device_id = device[0] if device else None
        platform = device[1] if device else "unknown"

        cur = db.execute(
            """
            INSERT INTO sessions (session_num, date, summary, tasks_completed, files_changed, bugs_found, notes, device_id, platform, device_name)
            VALUES (?, date('now'), ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                data.session_num,
                data.summary,
                json.dumps(data.tasks_completed) if data.tasks_completed else None,
                json.dumps(data.files_changed) if data.files_changed else None,
                json.dumps(data.bugs_found) if data.bugs_found else None,
                data.notes,
                device_id,
                platform,
                data.device_name,
            ),
        )
        db.commit()

        if device_id:
            db.execute("UPDATE devices SET last_seen=datetime('now') WHERE id=?", (device_id,))
            db.commit()

        asyncio.create_task(
            _fire_event(
                "session_created",
                {
                    "session_id": cur.lastrowid,
                    "device": data.device_name,
                },
            )
        )

        return {"id": cur.lastrowid, "session_num": data.session_num, "status": "created"}
    finally:
        db.close()
