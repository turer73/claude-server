"""Note (agent-to-agent mesaj) router handler'ları (memory paketi).

Gövdeler birebir taşındı (Faz 3).
"""

import asyncio

from fastapi import Depends, HTTPException

from app.api.memory import (
    NoteCreate,
    _ensure_read_by,
    _fire_event,
    _unread_pred,
    dispatch_origin,
    get_db,
    router,
)
from app.core.action_review import scan_dispatch_note
from app.core.events import emit_event
from app.core.privacy import redact


def _review_dispatch_note(from_device: str, to_device: str | None, content: str | None, note_id: int | None) -> None:
    """GAP-1 Kapsam-2: cross-agent dispatch notunu deterministik denetle (notify-only).

    FAIL-OPEN: bu fonksiyon ASLA raise ETMEZ — not zaten INSERT edildi, koordinasyon-kanali
    kritik. Scan-hata -> warn-emit, not durur.

    NOT (Codex P1 #1): built-in dispatcher (_send_to_surer) to_device SET ETMEZ (content-zarfinda
    alici='surer-sonnet' tasir). Bu yuzden to_device'a gate KOYULMAZ — her not scan_dispatch_note'a
    verilir; o, JSON-task-paketi degilse (duz-prose/broadcast) zaten benign doner (ucuz).
    """
    if not content:  # bos content = taranacak sey yok
        return
    try:
        result = scan_dispatch_note(content, from_device, to_device)
    except Exception as exc:  # noqa: BLE001 — FAIL-OPEN, not-yazimi bozulmaz
        emit_event(
            type="action-review",
            source="dispatch",
            title=f"dispatch-scan taranamadi: note#{note_id}",
            severity="warn",
            detail=f"{type(exc).__name__}: {exc}",
        )
        return
    if result["suspicious"]:
        emit_event(
            type="action-review",
            source="dispatch",
            title=f"cross-agent dispatch supheli: {from_device}->{to_device} note#{note_id}",
            severity="warn",
            detail="sinyaller: " + ", ".join(result["signals"]),
            payload={"note_id": note_id, **result},
        )


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
async def create_note(data: NoteCreate, forced_origin: str = Depends(dispatch_origin)):
    # Privacy + dedup
    # NOT: BEGIN IMMEDIATE ile race condition kapatildi (paralel POST iki
    # SELECT'inde de dup gormezken ikisi de INSERT eden senaryo — #169/#170
    # 9-saniye dup pattern'i).
    # GAP-1 item-D (#1222 A-2): otonom-key ile auth olduysa from_device ZORLA-override
    # ('klipper-autonomous') — body-claim gozardi (unforgeable). Normal-key -> body korunur.
    from_device = forced_origin or data.from_device
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
            (from_device, data.to_device, data.title, content_clean),
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
            (from_device, data.to_device, data.title),
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
            (from_device, data.to_device, data.title, content_clean),
        )
        db.commit()

        # GAP-1 Kapsam-2: cross-agent dispatch denetimi (notify-only + FAIL-OPEN; not ZATEN yazildi).
        _review_dispatch_note(from_device, data.to_device, content_clean, cur.lastrowid)

        asyncio.create_task(
            _fire_event(
                "note_created",
                {
                    "id": cur.lastrowid,
                    "from_device": from_device,
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
            # ATOMIK append-if-absent — eski SELECT→Python-modify→UPDATE lost-update
            # race'liydi (2 device eşzamanlı okununca biri düşerdi, #1226/#1228).
            # read_by invariantı '|dev1|dev2|' (boş='') → tek UPDATE ile serialize et.
            cur = db.execute(
                """UPDATE notes SET read_by =
                     CASE
                       WHEN read_by IS NULL OR read_by = '' THEN '|' || ? || '|'
                       WHEN instr(read_by, '|' || ? || '|') > 0 THEN read_by
                       ELSE read_by || ? || '|'
                     END
                   WHERE id = ?""",
                (device, device, device, note_id),
            )
            if cur.rowcount == 0:
                db.rollback()
                raise HTTPException(status_code=404, detail="note not found")
            db.commit()
            row = db.execute("SELECT read_by FROM notes WHERE id=?", (note_id,)).fetchone()
            devs = [d for d in (row[0] or "").strip("|").split("|") if d]
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
