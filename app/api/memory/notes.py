"""Note (agent-to-agent mesaj) router handler'ları (memory paketi).

Gövdeler birebir taşındı (Faz 3).
"""

import asyncio

from fastapi import HTTPException

from app.api.memory import NoteCreate, _ensure_read_by, _fire_event, _unread_pred, get_db, router
from app.core.privacy import redact


@router.get("/notes")
async def list_notes(device: str | None = None, unread_only: bool = False):
    db = get_db()
    try:
        _ensure_read_by(db)
        query = "SELECT * FROM notes WHERE 1=1"
        params = []
        if device:
            query += " AND (to_device=? OR to_device IS NULL)"
            params.append(device)
        if unread_only:
            # device verildiyse PER-DEVICE okunmamış, yoksa legacy global (#647)
            pred, pp = _unread_pred(device)
            query += f" AND {pred}"
            params.extend(pp)
        query += " ORDER BY created_at DESC LIMIT 50"
        return [dict(r) for r in db.execute(query, params).fetchall()]
    finally:
        db.close()


@router.post("/notes")
async def create_note(data: NoteCreate):
    # Privacy + dedup
    # NOT: BEGIN IMMEDIATE ile race condition kapatildi (paralel POST iki
    # SELECT'inde de dup gormezken ikisi de INSERT eden senaryo — #169/#170
    # 9-saniye dup pattern'i).
    content_clean, redacted_labels = redact(data.content)
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        # 1. Tam dup (content identical) — 5dk pencere
        recent_dup = db.execute(
            "SELECT id FROM notes WHERE from_device=? "
            "AND COALESCE(to_device,'')=COALESCE(?,'') "
            "AND title=? AND content=? "
            "AND created_at > datetime('now','-5 minutes')",
            (data.from_device, data.to_device, data.title, content_clean),
        ).fetchone()
        if recent_dup:
            db.rollback()
            return {
                "id": recent_dup[0],
                "status": "duplicate_skipped_5min",
                "secrets_redacted": redacted_labels,
            }

        # 2. Title-only soft dedup — 30sn cok-kisa pencere, race + double-fire
        # icin defansif. Content farkli olsa bile ayni title ayni from_device
        # 30sn icinde tekrar gelirse: ikinci handler invocation (Surer
        # autonomous handler double-fire) — bu API katmaninda durdur.
        title_dup = db.execute(
            "SELECT id FROM notes WHERE from_device=? "
            "AND COALESCE(to_device,'')=COALESCE(?,'') "
            "AND title=? "
            "AND created_at > datetime('now','-30 seconds')",
            (data.from_device, data.to_device, data.title),
        ).fetchone()
        if title_dup:
            db.rollback()
            return {
                "id": title_dup[0],
                "status": "duplicate_title_30s",
                "secrets_redacted": redacted_labels,
            }

        cur = db.execute(
            "INSERT INTO notes (from_device, to_device, title, content) VALUES (?, ?, ?, ?)",
            (data.from_device, data.to_device, data.title, content_clean),
        )
        db.commit()

        asyncio.create_task(
            _fire_event(
                "note_created",
                {
                    "id": cur.lastrowid,
                    "from_device": data.from_device,
                    "to_device": data.to_device,
                    "title": data.title,
                },
            )
        )

        return {"id": cur.lastrowid, "status": "created", "secrets_redacted": redacted_labels}
    finally:
        db.close()


@router.put("/notes/{note_id}/read")
async def mark_note_read(note_id: int, device: str | None = None):
    """Notu okundu işaretle. device verilirse PER-DEVICE (read_by'a eklenir, diğer
    device'lar için okunmamış kalır — #647). device yoksa LEGACY global read=1
    (geri-uyum: eski çağıranlar bozulmaz, ama çoğulcu-okuma kaybolur → device gönderin)."""
    db = get_db()
    try:
        _ensure_read_by(db)
        if device:
            row = db.execute("SELECT read_by FROM notes WHERE id=?", (note_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="note not found")
            devs = [d for d in (row[0] or "").strip("|").split("|") if d]
            if device not in devs:
                devs.append(device)
            new_rb = "|" + "|".join(devs) + "|" if devs else ""
            db.execute("UPDATE notes SET read_by=? WHERE id=?", (new_rb, note_id))
            db.commit()
            return {"status": "read", "device": device, "read_by": devs}
        db.execute("UPDATE notes SET read=1 WHERE id=?", (note_id,))
        db.commit()
        return {"status": "read"}
    finally:
        db.close()


@router.put("/notes/{note_id}/unread")
async def mark_note_unread(note_id: int):
    """Test/debug için: notu tekrar unread yap. Üretim akışında kullanılmaz."""
    db = get_db()
    try:
        cur = db.execute("UPDATE notes SET read=0 WHERE id=?", (note_id,))
        db.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="note not found")
        return {"status": "unread"}
    finally:
        db.close()
