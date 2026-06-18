"""Memory CRUD + surface/world-model router handler'ları (memory paketi).

Gövdeler birebir taşındı (Faz 3). _has_merged_into / _surface_query yalnız bu
domain'de kullanılır → birlikte taşındı.
"""

import asyncio

from fastapi import HTTPException, Query

from app.api.memory import MemoryCreate, MemoryUpdate, _track_read, get_db, router
from app.core.privacy import redact


@router.get("/memories")
async def list_memories(type: str | None = None, active: int = 1, search: str | None = None):
    db = get_db()
    try:
        query = "SELECT id, type, name, description, source_device, read_count, date(updated_at) as updated FROM memories WHERE active=?"
        params = [active]
        if type:
            query += " AND type=?"
            params.append(type)
        if search:
            query += " AND (content LIKE ? OR name LIKE ? OR description LIKE ?)"
            params.extend([f"%{search}%"] * 3)
        query += " ORDER BY type, updated_at DESC"
        return [dict(r) for r in db.execute(query, params).fetchall()]
    finally:
        db.close()


def _has_merged_into(db) -> bool:
    """merged_into kolonu var mı (LIVESYS-MEMSYN migration uygulanmış mı)."""
    return "merged_into" in [r[1] for r in db.execute("PRAGMA table_info(memories)").fetchall()]


def _surface_query(type: str | None, limit: int, offset: int):
    db = get_db()
    try:
        cond = "active=1" + (" AND merged_into IS NULL" if _has_merged_into(db) else "")
        wparams: list = []
        if type:
            cond += " AND type=?"
            wparams.append(type)
        total = db.execute(f"SELECT COUNT(*) FROM memories WHERE {cond}", wparams).fetchone()[0]
        q = (
            f"SELECT id, type, name, description, read_count, date(updated_at) AS updated "
            f"FROM memories WHERE {cond} ORDER BY type, updated_at DESC LIMIT ? OFFSET ?"
        )
        items = [dict(r) for r in db.execute(q, [*wparams, limit, offset]).fetchall()]
        return {"total": total, "count": len(items), "limit": limit, "offset": offset, "items": items}
    finally:
        db.close()


@router.get("/surface")
async def memory_surface(
    type: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Sentez-sonrası YÜZEY: aktif + canonical (merged olmayan) memory'ler (LIVESYS-MEMSYN).
    P0-c (surer): SAYFALANMIŞ — default 100 (max 500). 629-korpus limit'siz ~48K-token bomba
    (LLM-context'i doldurur). Yanıt {total, count, limit, offset, items}; items kapalı-uçlu.
    Faz 2: sync DB to_thread'e offload (event-loop blokmaz)."""
    return await asyncio.to_thread(_surface_query, type, limit, offset)


@router.get("/world-model")
async def memory_world_model():
    """Sentezlenmiş DÜNYA-MODELİ özeti: tür-bazlı yüzey sayımı + arşiv istatistiği (LIVESYS-MEMSYN)."""
    db = get_db()
    try:
        has_mi = _has_merged_into(db)
        surface_cond = "active=1" + (" AND merged_into IS NULL" if has_mi else "")
        by_type = {
            r["type"]: r["n"] for r in db.execute(f"SELECT type, COUNT(*) AS n FROM memories WHERE {surface_cond} GROUP BY type").fetchall()
        }
        surface = db.execute(f"SELECT COUNT(*) FROM memories WHERE {surface_cond}").fetchone()[0]
        active_total = db.execute("SELECT COUNT(*) FROM memories WHERE active=1").fetchone()[0]
        archived = db.execute("SELECT COUNT(*) FROM memories WHERE merged_into IS NOT NULL").fetchone()[0] if has_mi else 0
        return {
            "surface_by_type": by_type,
            "surface_total": surface,
            "active_total": active_total,
            "merged_archived": archived,
            "synthesized": has_mi,
        }
    finally:
        db.close()


@router.get("/memories/{memory_id}")
async def get_memory(memory_id: int):
    db = get_db()
    try:
        row = db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Memory not found")
        _track_read(db, "memories", memory_id)
        return dict(row)
    finally:
        db.close()


@router.post("/memories")
async def create_memory(data: MemoryCreate):
    # Privacy: secret/token strip
    desc_clean, desc_labels = redact(data.description)
    content_clean, content_labels = redact(data.content)
    redacted_labels = sorted(set(desc_labels) | set(content_labels))

    db = get_db()
    try:
        # 5-dakika exact-match dedup window (agentmemory pattern):
        # ayni name+description+content son 5 dk icinde varsa skip et.
        recent_dup = db.execute(
            "SELECT id FROM memories WHERE active=1 AND type=? AND name=? "
            "AND COALESCE(description,'')=? AND COALESCE(content,'')=? "
            "AND updated_at > datetime('now','-5 minutes')",
            (data.type, data.name, desc_clean or "", content_clean or ""),
        ).fetchone()
        if recent_dup:
            return {
                "id": recent_dup[0],
                "status": "duplicate_skipped_5min",
                "secrets_redacted": redacted_labels,
            }

        # Duplicate kontrolu (name+type bazli upsert — eski davranis)
        existing = db.execute("SELECT id FROM memories WHERE active=1 AND type=? AND name=?", (data.type, data.name)).fetchone()
        if existing:
            db.execute(
                "UPDATE memories SET description=?, content=?, source_device=?, rationale=COALESCE(?, rationale), updated_at=datetime('now') WHERE id=?",
                (desc_clean, content_clean, data.source_device, data.rationale, existing[0]),
            )
            db.commit()
            return {"id": existing[0], "status": "updated_existing", "secrets_redacted": redacted_labels}

        cur = db.execute(
            "INSERT INTO memories (type, name, description, content, source_device, rationale) VALUES (?, ?, ?, ?, ?, ?)",
            (data.type, data.name, desc_clean, content_clean, data.source_device, data.rationale),
        )
        db.commit()
        return {"id": cur.lastrowid, "status": "created", "secrets_redacted": redacted_labels}
    finally:
        db.close()


@router.put("/memories/{memory_id}")
async def update_memory(memory_id: int, data: MemoryUpdate):
    db = get_db()
    try:
        fields, params = [], []
        for field in ["name", "description", "content", "active"]:
            val = getattr(data, field)
            if val is not None:
                fields.append(f"{field}=?")
                params.append(val)
        if not fields:
            raise HTTPException(400, "No fields to update")
        fields.append("updated_at=datetime('now')")
        params.append(memory_id)
        db.execute(f"UPDATE memories SET {', '.join(fields)} WHERE id=?", params)
        db.commit()
        return {"status": "updated"}
    finally:
        db.close()


@router.delete("/memories/{memory_id}")
async def deactivate_memory(memory_id: int):
    db = get_db()
    try:
        db.execute("UPDATE memories SET active=0, updated_at=datetime('now') WHERE id=?", (memory_id,))
        db.commit()
        return {"status": "deactivated"}
    finally:
        db.close()
