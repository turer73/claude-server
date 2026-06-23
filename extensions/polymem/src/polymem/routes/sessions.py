"""Sessions router (append-only log of agent work)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status

from polymem.db import connect
from polymem.models import SessionCreate, SessionRead


def _row_to_session(row) -> dict:
    data = dict(row)
    raw = data.get("metadata")
    data["metadata"] = json.loads(raw) if raw else None
    return data


def build_router(db_path: str | Path, auth_dep) -> APIRouter:
    router = APIRouter(prefix="/sessions", tags=["sessions"], dependencies=[Depends(auth_dep)])

    @router.get("", response_model=list[SessionRead])
    async def list_sessions(
        device: str | None = None,
        project: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = Query(default=30, ge=1, le=500),
    ):
        clauses, params = [], []
        if device is not None:
            clauses.append("device_name = ?")
            params.append(device)
        if project is not None:
            clauses.append("project = ?")
            params.append(project)
        if date_from is not None:
            clauses.append("date >= ?")
            params.append(date_from)
        if date_to is not None:
            clauses.append("date <= ?")
            params.append(date_to)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM sessions {where} ORDER BY date DESC, id DESC LIMIT ?"
        params.append(limit)
        with connect(db_path) as db:
            rows = db.execute(sql, params).fetchall()
        return [_row_to_session(r) for r in rows]

    @router.get("/{session_id}", response_model=SessionRead)
    async def get_session(session_id: int):
        with connect(db_path) as db:
            row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="session not found")
        return _row_to_session(row)

    @router.post("", response_model=SessionRead, status_code=status.HTTP_201_CREATED)
    async def create_session(data: SessionCreate):
        metadata_json = json.dumps(data.metadata) if data.metadata is not None else None
        with connect(db_path) as db:
            if data.date is not None:
                cursor = db.execute(
                    """
                    INSERT INTO sessions (device_name, project, date, summary, metadata)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (data.device_name, data.project, data.date, data.summary, metadata_json),
                )
            else:
                cursor = db.execute(
                    """
                    INSERT INTO sessions (device_name, project, summary, metadata)
                    VALUES (?, ?, ?, ?)
                    """,
                    (data.device_name, data.project, data.summary, metadata_json),
                )
            db.commit()
            row = db.execute("SELECT * FROM sessions WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _row_to_session(row)

    @router.delete("/{session_id}", status_code=status.HTTP_200_OK)
    async def delete_session(session_id: int):
        with connect(db_path) as db:
            cursor = db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="session not found")
        return {"id": session_id, "deleted": True}

    return router
