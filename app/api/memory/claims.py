"""P1 CLAIM-lock — çakışma önleme not-konvansiyonundan DB-kısıtına (konu-1 kararı).

Bugün iki kez kanıtlandı: nezakete dayalı CLAIM-notu paralel-implementasyonu önlemiyor
(PR#301 surer+klipper aynı fix; disc#1288-1290 üç-ajan çakışması). Çözüm klipper #100549
şeması: active_claims + partial-UNIQUE(task_key WHERE active=1) → acquire ATOMIK, ikinci
gelen 409 + mevcut-claim bilgisi alır. TTL lazy-expiry (CLAIM-protokolü 4h varsayılan).

Sahiplik: release/renew yalnız sahibi (device-key kimliği) veya master. repo+branch
alanları CI-gate botu (claim-status-poller) içindir — PR-branch ↔ claim eşleşmesi.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import Depends, Header, HTTPException

from app.api import memory as _mem
from app.api.memory import (
    ClaimCreate,
    dispatch_origin,
    get_db,
    router,
)

_claims_ready = False


def _ensure_claims(db: Any) -> None:
    """active_claims tablosunu idempotent kur (_ensure_read_by deseni).
    Partial-UNIQUE: ayni task_key icin AYNI ANDA tek aktif claim (dogruluk-kisiti,
    SQLite WHERE'li unique-index destekler)."""
    global _claims_ready
    if _claims_ready:
        return
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS active_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_key TEXT NOT NULL,
            device TEXT NOT NULL,
            repo TEXT,
            branch TEXT,
            note TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            released_at TEXT
        )""")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_active_claims_key ON active_claims(task_key) WHERE active=1")
        db.commit()
        # Codex#303-3tur: flag YALNIZ basarida (desen 3. kez — ortak ensure-helper'a cikarma
        # onerisi rebase-sonrasi konsolidasyon commit'ine notlandi)
        _claims_ready = True
    except Exception:
        pass


def _expire_stale(db: Any) -> None:
    """TTL dolan claim'leri lazy-release (crash-korumasi: 4h yenilenmeyen claim bayat)."""
    db.execute("UPDATE active_claims SET active=0, released_at=datetime('now') WHERE active=1 AND expires_at < datetime('now')")


def _claim_device(data_device: str, forced_origin: str) -> str:
    """Claim kimliği: device-key/otonom auth -> KEY kazanir (unforgeable, P0 deseni);
    master-key legacy -> body-device (dogrulanmamis ama calisir, kademeli gecis)."""
    return forced_origin or data_device


@router.post("/claims")
async def acquire_claim(data: ClaimCreate, forced_origin: str = Depends(dispatch_origin)) -> dict[str, Any]:
    device = _claim_device(data.device, forced_origin)
    db = get_db()
    try:
        _ensure_claims(db)
        db.execute("BEGIN IMMEDIATE")
        _expire_stale(db)
        try:
            cur = db.execute(
                "INSERT INTO active_claims (task_key, device, repo, branch, note, expires_at) VALUES (?, ?, ?, ?, ?, datetime('now', ?))",
                (data.task_key, device, data.repo, data.branch, data.note, f"+{data.ttl_hours} hours"),
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            holder = db.execute(
                "SELECT id, device, created_at, expires_at, note FROM active_claims WHERE task_key=? AND active=1",
                (data.task_key,),
            ).fetchone()
            raise HTTPException(
                409,
                {
                    "error": "task_key zaten claim'li",
                    "holder": dict(holder) if holder else None,
                    "hint": "Sahibiyle koordine et ya da TTL dolmasini bekle (release/renew sahibinde)",
                },
            ) from None
        return {"id": cur.lastrowid, "task_key": data.task_key, "device": device, "status": "acquired", "verified": bool(forced_origin)}
    finally:
        db.close()


def _load_active_claim(db: Any, claim_id: int) -> Any:
    row = db.execute("SELECT * FROM active_claims WHERE id=? AND active=1", (claim_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Aktif claim bulunamadi (release edilmis ya da TTL dolmus olabilir)")
    return row


def _require_owner(row: Any, device: str, x_memory_key: str | None) -> None:
    """Sahiplik: claim'in device'i VEYA master/admin-key. Baska cihaz 403 — claim baskasinin
    isini kapatamaz (kimlik-zorlamasi P0 ile anlamli; master/admin = insan-mudahale kacis-kapisi).
    Codex#303-3tur: admin-key de kabul — dispatch_origin admin'de '' dondugu icin admin'in
    actigi claim body-device'a duser, admin kendi claim'ini kapatamiyordu (fazla-kisitlayici)."""
    # Modul-referansiyla oku (from-import snapshot'i test-monkeypatch'i ve runtime
    # key-rotation'i gormez — verify_master_key ile ayni kaynak)
    if x_memory_key == _mem.MEMORY_API_KEY or _mem._is_admin_key(x_memory_key):
        return
    if device and device == row["device"]:
        return
    raise HTTPException(403, f"Claim sahibi '{row['device']}' — sen '{device or 'bilinmiyor'}'. Release/renew sahibinde (ya da master/admin).")


@router.put("/claims/{claim_id}/release")
async def release_claim(claim_id: int, forced_origin: str = Depends(dispatch_origin), x_memory_key: str = Header(None)) -> dict[str, Any]:
    # Codex#303-P2 sinifi (renew ile ayni desen, proaktif): expiry+read+owner+update tek
    # BEGIN IMMEDIATE — read ile UPDATE arasinda TTL-dolumu/yeniden-acquire race'i kapali.
    # 404/403 raise'lerinde acik transaction close()'ta rollback olur (lazy-expiry yeniden kosar).
    db = get_db()
    try:
        _ensure_claims(db)
        db.execute("BEGIN IMMEDIATE")
        _expire_stale(db)
        try:
            row = _load_active_claim(db, claim_id)
        except HTTPException:
            db.commit()  # lazy-expiry sweep'i 404'te de kalici (eski gozlemlenir davranis)
            raise
        _require_owner(row, forced_origin, x_memory_key)
        cur = db.execute("UPDATE active_claims SET active=0, released_at=datetime('now') WHERE id=? AND active=1", (claim_id,))
        if cur.rowcount == 0:
            db.rollback()
            raise HTTPException(409, "Claim release edilemedi (es-zamanli expire/release) — durumu yeniden sorgula")
        db.commit()
        return {"id": claim_id, "status": "released"}
    finally:
        db.close()


@router.put("/claims/{claim_id}/renew")
async def renew_claim(
    claim_id: int, ttl_hours: float = 4.0, forced_origin: str = Depends(dispatch_origin), x_memory_key: str = Header(None)
) -> dict[str, Any]:
    if not (0.1 <= ttl_hours <= 72):
        raise HTTPException(422, "ttl_hours 0.1-72 araliginda olmali")
    # Codex#303-P2: renew atomik degildi — read ile UPDATE arasinda TTL dolup baskasi ayni
    # task_key'i yeniden acquire ederse, eski sahip inactive satiri guncelleyip sahte 'renewed'
    # aliyordu (kilit baskasindayken yanlis-guven). Fix: acquire'daki gibi tek BEGIN IMMEDIATE
    # + final UPDATE'e AND active=1 + rowcount=0 -> 409.
    db = get_db()
    try:
        _ensure_claims(db)
        db.execute("BEGIN IMMEDIATE")
        _expire_stale(db)
        try:
            row = _load_active_claim(db, claim_id)
        except HTTPException:
            db.commit()  # lazy-expiry sweep'i 404'te de kalici (eski gozlemlenir davranis)
            raise
        _require_owner(row, forced_origin, x_memory_key)
        cur = db.execute(
            "UPDATE active_claims SET expires_at=datetime('now', ?) WHERE id=? AND active=1",
            (f"+{ttl_hours} hours", claim_id),
        )
        if cur.rowcount == 0:
            db.rollback()
            raise HTTPException(409, "Claim yenilenemedi (es-zamanli expire/release) — yeniden acquire et")
        db.commit()
        return {"id": claim_id, "status": "renewed", "ttl_hours": ttl_hours}
    finally:
        db.close()


@router.get("/claims")
async def list_claims(repo: str | None = None, branch: str | None = None, include_released: bool = False) -> dict[str, Any]:
    """Aktif claim listesi (CI-gate botu repo+branch ile sorgular). Salt-okunur."""
    db = get_db()
    try:
        _ensure_claims(db)
        _expire_stale(db)
        db.commit()
        q = "SELECT * FROM active_claims WHERE 1=1"
        params: list[Any] = []
        if not include_released:
            q += " AND active=1"
        if repo:
            q += " AND repo=?"
            params.append(repo)
        if branch:
            q += " AND branch=?"
            params.append(branch)
        rows = db.execute(q + " ORDER BY created_at DESC LIMIT 100", params).fetchall()
        return {"claims": [dict(r) for r in rows]}
    finally:
        db.close()
