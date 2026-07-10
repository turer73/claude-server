"""Note (agent-to-agent mesaj) router handler'ları (memory paketi).

Gövdeler birebir taşındı (Faz 3).
"""

import asyncio
from typing import Any

from fastapi import Depends, HTTPException

from app.api.memory import (
    NoteCreate,
    _ensure_read_by,
    _ensure_status,
    _ensure_verified,
    _fire_event,
    _unread_pred,
    dispatch_origin,
    get_db,
    router,
    verify_master_key,
)
from app.core.action_review import (
    _is_autonomous_origin,
    dispatch_policy_gate_enabled,
    scan_dispatch_note,
)
from app.core.events import emit_event
from app.core.privacy import redact


def _gate_dispatch(from_device: str, to_device: str | None, content: str | None) -> tuple[str, dict[str, Any] | None]:
    """Policy-gate #1222: cross-agent dispatch'i INSERT'ten ONCE denetle -> (status, scan_result).

    status='held' YALNIZ: gate-ON + suspicious + otonom-origin (interaktif klipper/surer ASLA held —
    #1222 tam da "otonom insan-gate'i atliyor" sorunu; from_device unforgeable A-2). Aksi -> 'active'.
    FAIL-OPEN (tasarim §3.4): scan-exception -> ('active', None) + warn-emit. Koordinasyon-kanali
    omurga; gate-bug'i kanali BRICKLEMEZ. Bos-content -> ('active', None) (taranacak sey yok).

    NOT (Codex P1 #1): built-in dispatcher (_send_to_surer) to_device SET ETMEZ (content-zarfinda
    alici tasir); scan_dispatch_note zarfi da tarar, JSON-task-paketi degilse benign doner (ucuz).
    """
    if not content:
        return "active", None
    try:
        result = scan_dispatch_note(content, from_device, to_device)
    except Exception as exc:  # noqa: BLE001 — FAIL-OPEN, not-yazimi bozulmaz
        emit_event(
            type="action-review",
            source="dispatch",
            title=f"dispatch-scan taranamadi (fail-open): {from_device}->{to_device}",
            severity="warn",
            detail=f"{type(exc).__name__}: {exc}",
        )
        return "active", None
    held = dispatch_policy_gate_enabled() and result["suspicious"] and _is_autonomous_origin(from_device)
    return ("held" if held else "active"), result


def _emit_dispatch_verdict(
    from_device: str, to_device: str | None, note_id: int | None, status: str, scan_result: dict[str, Any] | None
) -> None:
    """Post-insert emit (note_id'li). held -> CRITICAL (insan-onayi bekliyor); gate-OFF-suspicious ->
    WARN (shadow: notify-only, mevcut davranis). would_hold (payload): gate-durumundan BAGIMSIZ
    'suspicious + otonom' -> shadow-haftada FP-analizi icin (OFF'ta kac dispatch HELD-edilir-DI)."""
    if scan_result is None or not scan_result["suspicious"]:
        return
    held = status == "held"
    would_hold = scan_result["suspicious"] and _is_autonomous_origin(from_device)
    emit_event(
        type="action-review",
        source="dispatch",
        title=("cross-agent dispatch HELD (insan-onayi bekliyor): " if held else "cross-agent dispatch supheli: ")
        + f"{from_device}->{to_device} note#{note_id}",
        severity="critical" if held else "warn",
        detail="sinyaller: " + ", ".join(scan_result["signals"]),
        payload={
            "note_id": note_id,
            "status": status,
            "policy_gate": dispatch_policy_gate_enabled(),
            "would_hold": would_hold,
            **scan_result,
        },
    )


@router.get("/notes")
async def list_notes(device: str | None = None, unread_only: bool = False):
    db = get_db()
    try:
        _ensure_read_by(db)
        _ensure_status(db)
        query = "SELECT * FROM notes WHERE 1=1"
        params = []
        if device:
            query += " AND (to_device=? OR to_device IS NULL)"
            params.append(device)
            # Policy-gate #1222 teslim-filtresi: held/rejected notlar ALICIYA teslim EDILMEZ
            # (yalniz active). device-SIZ cagri = MASTER/genel view -> filtre YOK (held gorunur,
            # onay-listesi + dashboard icin). COALESCE: eski NULL-satirlar 'active' sayilir (geri-uyum).
            query += " AND COALESCE(status,'active')='active'"
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
    # P0 kimlik: forced_origin dolu = kimlik KEY'den turetildi (device-key/otonom) -> verified.
    # Bos = legacy master-key, body-iddiasina dusuluyor -> unverified (durust etiket).
    verified = 1 if forced_origin else 0
    content_clean, redacted_labels = redact(data.content)
    db = get_db()
    try:
        _ensure_status(db)  # policy-gate #1222 migration (idempotent; BEGIN'den ONCE — ALTER+commit)
        _ensure_verified(db)  # P0 kimlik migration (idempotent)
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

        # Policy-gate #1222: scan-BEFORE-insert (tasarim §3.3). gate-ON+suspicious+otonom -> 'held'
        # (aliciya teslim YOK, insan-onayi bekler). gate-OFF/interaktif/benign -> 'active'. FAIL-OPEN.
        note_status, scan_result = _gate_dispatch(from_device, data.to_device, content_clean)

        cur = db.execute(
            "INSERT INTO notes (from_device, to_device, title, content, status, verified) VALUES (?, ?, ?, ?, ?, ?)",
            (from_device, data.to_device, data.title, content_clean, note_status, verified),
        )
        db.commit()

        _emit_dispatch_verdict(from_device, data.to_device, cur.lastrowid, note_status, scan_result)

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

        # held -> response'ta da 'held' (cagiran spawn dispatch'in bekletildigini bilir; teslim yok).
        return {
            "id": cur.lastrowid,
            "status": "held" if note_status == "held" else "created",
            "secrets_redacted": redacted_labels,
        }
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


@router.put("/notes/{note_id}/approve", dependencies=[Depends(verify_master_key)])
async def approve_note(note_id: int) -> dict[str, Any]:
    """Policy-gate #1222: held dispatch'i MASTER onayi ile RELEASE et -> status='active' (teslim-edilebilir,
    restore edilen insan-gate). MASTER-key ZORUNLU (verify_master_key route-dependency); otonom-key 401
    (self-approval baypasi engeli, §4 — otonom-ajan KENDI held-dispatch'ini onaylayamaz). Yalniz
    'held' -> 'active' gecisi (idempotent-degil: zaten-active/rejected -> 404, yanlis-onay engellenir)."""
    db = get_db()
    try:
        _ensure_status(db)
        cur = db.execute("UPDATE notes SET status='active' WHERE id=? AND status='held'", (note_id,))
        db.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="held note not found (yok veya zaten active/rejected)")
        return {"status": "approved", "note_id": note_id}
    finally:
        db.close()


@router.put("/notes/{note_id}/reject", dependencies=[Depends(verify_master_key)])
async def reject_note(note_id: int) -> dict[str, Any]:
    """Policy-gate #1222: held dispatch'i KALICI-REDDET -> status='rejected' (teslim YOK, audit-kaydi
    durur). MASTER-key ZORUNLU (otonom-key 401). Yalniz 'held' -> 'rejected' (zaten-terminal -> 404)."""
    db = get_db()
    try:
        _ensure_status(db)
        cur = db.execute("UPDATE notes SET status='rejected' WHERE id=? AND status='held'", (note_id,))
        db.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="held note not found (yok veya zaten active/rejected)")
        return {"status": "rejected", "note_id": note_id}
    finally:
        db.close()
