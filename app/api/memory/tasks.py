"""Task-log router handler'ları (memory paketi). Gövdeler birebir taşındı (Faz 3)."""

import asyncio
import json

from fastapi import HTTPException

from app.api.memory import TaskLogCreate, TaskLogUpdate, _fire_event, get_db, router


@router.get("/tasks")
async def list_tasks(project: str | None = None, device: str | None = None, limit: int = 30):
    db = get_db()
    try:
        query = "SELECT id, session_id, device_name, project, task, status, rationale, date(created_at) as date FROM tasks_log WHERE 1=1"
        params = []
        if project:
            query += " AND project=?"
            params.append(project)
        if device:
            query += " AND device_name=?"
            params.append(device)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in db.execute(query, params).fetchall()]
    finally:
        db.close()


@router.post("/tasks")
async def create_task_log(data: TaskLogCreate):
    db = get_db()
    try:
        # Duplicate kontrolü — aynı proje + aynı task adı
        existing = db.execute("SELECT id FROM tasks_log WHERE project=? AND task=?", (data.project, data.task)).fetchone()
        if existing:
            return {"id": existing[0], "status": "already_exists"}

        cur = db.execute(
            """
            INSERT INTO tasks_log (session_id, device_name, project, task, status, files_changed, details, rationale)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                data.session_id,
                data.device_name,
                data.project,
                data.task,
                data.status,
                json.dumps(data.files_changed) if data.files_changed else None,
                data.details,
                data.rationale,
            ),
        )
        db.commit()

        asyncio.create_task(
            _fire_event(
                "task_created",
                {
                    "id": cur.lastrowid,
                    "project": data.project,
                    "task": data.task,
                    "status": data.status,
                    "device": data.device_name,
                },
            )
        )

        return {"id": cur.lastrowid, "status": "created"}
    finally:
        db.close()


@router.patch("/tasks/{task_id}")
async def update_task_log(task_id: int, data: TaskLogUpdate):
    if data.status is None and data.rationale is None:
        raise HTTPException(400, "En az status veya rationale gönderin")
    db = get_db()
    try:
        row = db.execute("SELECT id, status FROM tasks_log WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Task {task_id} bulunamadı")
        sets, params = [], []
        if data.status is not None:
            sets.append("status=?")
            params.append(data.status)
        if data.rationale is not None:
            sets.append("rationale=?")
            params.append(data.rationale)
        params.append(task_id)
        db.execute(f"UPDATE tasks_log SET {', '.join(sets)} WHERE id=?", params)
        db.commit()
        new_status = data.status if data.status is not None else row["status"]
        return {"id": task_id, "new_status": new_status, "status": "updated"}
    finally:
        db.close()
