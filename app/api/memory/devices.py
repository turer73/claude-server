"""Device router handler'ları (memory paketi).

Kernel (router/get_db/modeller) app.api.memory __init__'ten import edilir;
handler gövdeleri taşınırken birebir korundu (davranış değişmez, Faz 3).
"""

import hashlib
import secrets

from fastapi import Depends

from app.api.memory import DeviceRegister, _ensure_device_keys, get_db, router, verify_master_key


@router.get("/devices")
async def list_devices():
    db = get_db()
    try:
        return [dict(r) for r in db.execute("SELECT * FROM devices ORDER BY last_seen DESC").fetchall()]
    finally:
        db.close()


@router.post("/devices")
async def register_device(data: DeviceRegister):
    db = get_db()
    try:
        db.execute(
            """
            INSERT INTO devices (name, platform, hostname, ip, tailscale_ip, os_version, claude_version, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                platform=excluded.platform, hostname=excluded.hostname, ip=excluded.ip,
                tailscale_ip=excluded.tailscale_ip, os_version=excluded.os_version,
                claude_version=excluded.claude_version, notes=excluded.notes,
                last_seen=datetime('now')
        """,
            (data.name, data.platform, data.hostname, data.ip, data.tailscale_ip, data.os_version, data.claude_version, data.notes),
        )
        db.commit()
        return {"status": "ok", "device": data.name}
    finally:
        db.close()


@router.post("/devices/{name}/key", dependencies=[Depends(verify_master_key)])
async def mint_device_key(name: str):
    """P0 kimlik: per-device API-key uret/rotate — MASTER-key ZORUNLU (otonom/device-key
    REDDEDILIR; onboarding-leak dersi: yanit yalniz YENI device'in key'ini icerir, master
    asla gomulmez). Ayni cihaza tekrar cagri = rotate (eski key aninda gecersiz).
    Key yalniz bu yanitta gorunur; DB'de sha256-hash saklanir."""
    key = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    db = get_db()
    try:
        _ensure_device_keys(db)
        db.execute(
            "INSERT INTO device_keys (device, key_hash, active) VALUES (?, ?, 1) "
            "ON CONFLICT(device) DO UPDATE SET key_hash=excluded.key_hash, active=1, "
            "created_at=datetime('now'), last_used_at=NULL",
            (name, key_hash),
        )
        db.commit()
        return {"device": name, "key": key, "note": "Key yalniz bu yanitta gosterilir; guvenli kanalla cihaza ilet"}
    finally:
        db.close()


@router.delete("/devices/{name}/key", dependencies=[Depends(verify_master_key)])
async def revoke_device_key(name: str):
    """Device-key iptal (active=0) — MASTER-key zorunlu. Cihaz master'a dusmez, 401 alir."""
    db = get_db()
    try:
        _ensure_device_keys(db)
        cur = db.execute("UPDATE device_keys SET active=0 WHERE device=? AND active=1", (name,))
        db.commit()
        return {"device": name, "revoked": cur.rowcount > 0}
    finally:
        db.close()


@router.post("/devices/{name}/ping")
async def ping_device(name: str):
    db = get_db()
    try:
        db.execute("UPDATE devices SET last_seen=datetime('now') WHERE name=?", (name,))
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()
