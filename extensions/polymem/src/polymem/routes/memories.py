"""Memories CRUD router."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status

from polymem.db import connect
from polymem.models import MemoryCreate, MemoryRead, MemoryType, MemoryUpdate


def build_router(db_path: str | Path, auth_dep) -> APIRouter:
    router = APIRouter(prefix="/memories", tags=["memories"], dependencies=[Depends(auth_dep)])

    @router.get("", response_model=list[MemoryRead])
    async def list_memories(
        type: MemoryType | None = None,
        active: int = 1,
        device: str | None = None,
        limit: int = Query(default=30, ge=1, le=500),
    ):
        clauses, params = ["active = ?"], [active]
        if type is not None:
            clauses.append("type = ?")
            params.append(type)
        if device is not None:
            clauses.append("source_device = ?")
            params.append(device)
        sql = (
            "SELECT * FROM memories WHERE "
            + " AND ".join(clauses)
            + " ORDER BY updated_at DESC LIMIT ?"
        )
        params.append(limit)
        with connect(db_path) as db:
            rows = db.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    @router.get("/{memory_id}", response_model=MemoryRead)
    async def get_memory(memory_id: int):
        with connect(db_path) as db:
            row = db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="memory not found")
        return dict(row)

    @router.post("", response_model=MemoryRead, status_code=status.HTTP_201_CREATED)
    async def create_memory(data: MemoryCreate):
        with connect(db_path) as db:
            cursor = db.execute(
                """
                INSERT INTO memories (type, name, description, content, source_device, rationale)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (data.type, data.name, data.description, data.content, data.source_device, data.rationale),
            )
            db.commit()
            row = db.execute("SELECT * FROM memories WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    @router.put("/{memory_id}", response_model=MemoryRead)
    async def update_memory(memory_id: int, data: MemoryUpdate):
        fields = data.model_dump(exclude_none=True)
        if not fields:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="no fields to update")
        assignments = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = datetime('now')"
        params = list(fields.values()) + [memory_id]
        with connect(db_path) as db:
            cursor = db.execute(f"UPDATE memories SET {assignments} WHERE id = ?", params)
            db.commit()
            if cursor.rowcount == 0:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="memory not found")
            row = db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return dict(row)

    @router.delete("/{memory_id}", status_code=status.HTTP_200_OK)
    async def deactivate_memory(memory_id: int):
        with connect(db_path) as db:
            cursor = db.execute(
                "UPDATE memories SET active = 0, updated_at = datetime('now') WHERE id = ?",
                (memory_id,),
            )
            db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="memory not found")
        return {"id": memory_id, "active": 0}

    @router.put("/{memory_id}/read", response_model=MemoryRead)
    async def mark_read(memory_id: int):
        with connect(db_path) as db:
            cursor = db.execute(
                """
                UPDATE memories
                SET read_count = read_count + 1, last_read_at = datetime('now')
                WHERE id = ?
                """,
                (memory_id,),
            )
            db.commit()
            if cursor.rowcount == 0:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="memory not found")
            row = db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return dict(row)

    return router
