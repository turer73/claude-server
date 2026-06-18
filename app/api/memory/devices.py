"""Device router handler'ları (memory paketi).

Kernel (router/get_db/modeller) app.api.memory __init__'ten import edilir;
handler gövdeleri taşınırken birebir korundu (davranış değişmez, Faz 3).
"""

from app.api.memory import DeviceRegister, get_db, router


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


@router.post("/devices/{name}/ping")
async def ping_device(name: str):
    db = get_db()
    try:
        db.execute("UPDATE devices SET last_seen=datetime('now') WHERE name=?", (name,))
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()
