"""
Claude Memory API — Multi-device hafıza sistemi
Tüm cihazlardan (Linux, Windows, Android) erişilebilir.
"""
import os
import sqlite3
import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, Header
from pydantic import BaseModel
from typing import Optional

DB_PATH = "/opt/linux-ai-server/data/claude_memory.db"

# Load from .env if not in environment
MEMORY_API_KEY = os.environ.get("MEMORY_API_KEY", "")
if not MEMORY_API_KEY:
    _env_path = "/opt/linux-ai-server/.env"
    if os.path.exists(_env_path):
        with open(_env_path) as _f:
            for _line in _f:
                if _line.startswith("MEMORY_API_KEY="):
                    MEMORY_API_KEY = _line.strip().split("=", 1)[1]
                    break


def verify_key(x_memory_key: str = Header(None)):
    if MEMORY_API_KEY and x_memory_key != MEMORY_API_KEY:
        raise HTTPException(401, "Invalid memory API key")


router = APIRouter(prefix="/api/v1/memory", tags=["memory"], dependencies=[Depends(verify_key)])
public_router = APIRouter(prefix="/api/v1/memory", tags=["memory-public"])


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ============ Models ============

class DeviceRegister(BaseModel):
    name: str
    platform: str  # linux, windows, android
    hostname: Optional[str] = None
    ip: Optional[str] = None
    tailscale_ip: Optional[str] = None
    os_version: Optional[str] = None
    claude_version: Optional[str] = None
    notes: Optional[str] = None

class SessionCreate(BaseModel):
    device_name: str
    session_num: Optional[int] = None
    summary: str
    tasks_completed: Optional[list] = None
    files_changed: Optional[list] = None
    bugs_found: Optional[list] = None
    notes: Optional[str] = None

class MemoryCreate(BaseModel):
    type: str  # user, feedback, project, reference
    name: str
    description: str
    content: str
    source_device: Optional[str] = "klipper"

class MemoryUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    content: Optional[str] = None
    active: Optional[int] = None

class TaskLogCreate(BaseModel):
    session_id: Optional[int] = None
    device_name: Optional[str] = "klipper"
    project: str
    task: str
    status: Optional[str] = "completed"
    files_changed: Optional[list] = None
    details: Optional[str] = None

class DiscoveryCreate(BaseModel):
    session_id: Optional[int] = None
    device_name: Optional[str] = "klipper"
    project: str
    type: str  # bug, fix, learning, config, workaround
    title: str
    details: Optional[str] = None
    resolved: Optional[int] = 0

class NoteCreate(BaseModel):
    from_device: str
    to_device: Optional[str] = None  # None = broadcast
    title: str
    content: str

class DeviceProjectCreate(BaseModel):
    device_name: str
    project: str
    local_path: Optional[str] = None


# ============ Dashboard / Overview ============

@router.get("/dashboard")
async def memory_dashboard():
    """Tüm cihazların özet durumu"""
    db = get_db()
    try:
        stats = {
            "memories": db.execute("SELECT COUNT(*) FROM memories WHERE active=1").fetchone()[0],
            "sessions": db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            "tasks": db.execute("SELECT COUNT(*) FROM tasks_log").fetchone()[0],
            "discoveries": db.execute("SELECT COUNT(*) FROM discoveries").fetchone()[0],
            "open_bugs": db.execute("SELECT COUNT(*) FROM discoveries WHERE type='bug' AND resolved=0").fetchone()[0],
            "unread_notes": db.execute("SELECT COUNT(*) FROM notes WHERE read=0").fetchone()[0],
        }

        devices = [dict(r) for r in db.execute(
            "SELECT name, platform, hostname, tailscale_ip, last_seen FROM devices ORDER BY last_seen DESC"
        ).fetchall()]

        recent_sessions = [dict(r) for r in db.execute(
            "SELECT session_num, date, device_name, platform, substr(summary,1,100) as summary "
            "FROM sessions ORDER BY id DESC LIMIT 5"
        ).fetchall()]

        open_bugs = [dict(r) for r in db.execute(
            "SELECT project, title, device_name, created_at FROM discoveries "
            "WHERE type='bug' AND resolved=0 ORDER BY created_at DESC"
        ).fetchall()]

        return {
            "stats": stats,
            "devices": devices,
            "recent_sessions": recent_sessions,
            "open_bugs": open_bugs
        }
    finally:
        db.close()


# ============ Devices ============

@router.get("/devices")
async def list_devices():
    db = get_db()
    try:
        rows = db.execute("SELECT * FROM devices ORDER BY last_seen DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()

@router.post("/devices")
async def register_device(data: DeviceRegister):
    db = get_db()
    try:
        db.execute("""
            INSERT INTO devices (name, platform, hostname, ip, tailscale_ip, os_version, claude_version, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                platform=excluded.platform, hostname=excluded.hostname, ip=excluded.ip,
                tailscale_ip=excluded.tailscale_ip, os_version=excluded.os_version,
                claude_version=excluded.claude_version, notes=excluded.notes,
                last_seen=datetime('now')
        """, (data.name, data.platform, data.hostname, data.ip,
              data.tailscale_ip, data.os_version, data.claude_version, data.notes))
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
        return {"status": "ok", "last_seen": datetime.utcnow().isoformat()}
    finally:
        db.close()


# ============ Memories ============

@router.get("/memories")
async def list_memories(
    type: Optional[str] = None,
    active: int = 1,
    search: Optional[str] = None
):
    db = get_db()
    try:
        query = "SELECT id, type, name, description, source_device, date(updated_at) as updated FROM memories WHERE active=?"
        params = [active]
        if type:
            query += " AND type=?"
            params.append(type)
        if search:
            query += " AND (content LIKE ? OR name LIKE ? OR description LIKE ?)"
            params.extend([f"%{search}%"] * 3)
        query += " ORDER BY type, updated_at DESC"
        rows = db.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()

@router.get("/memories/{memory_id}")
async def get_memory(memory_id: int):
    db = get_db()
    try:
        row = db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Memory not found")
        return dict(row)
    finally:
        db.close()

@router.post("/memories")
async def create_memory(data: MemoryCreate):
    db = get_db()
    try:
        cur = db.execute("""
            INSERT INTO memories (type, name, description, content, source_device)
            VALUES (?, ?, ?, ?, ?)
        """, (data.type, data.name, data.description, data.content, data.source_device))
        db.commit()
        return {"id": cur.lastrowid, "status": "created"}
    finally:
        db.close()

@router.put("/memories/{memory_id}")
async def update_memory(memory_id: int, data: MemoryUpdate):
    db = get_db()
    try:
        fields = []
        params = []
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


# ============ Sessions ============

@router.get("/sessions")
async def list_sessions(
    device: Optional[str] = None,
    platform: Optional[str] = None,
    limit: int = 20
):
    db = get_db()
    try:
        query = "SELECT id, session_num, date, device_name, platform, summary FROM sessions WHERE 1=1"
        params = []
        if device:
            query += " AND device_name=?"
            params.append(device)
        if platform:
            query += " AND platform=?"
            params.append(platform)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()

@router.get("/sessions/{session_id}")
async def get_session(session_id: int):
    db = get_db()
    try:
        session = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            raise HTTPException(404, "Session not found")
        result = dict(session)
        # İlişkili task ve discovery'ler
        tasks = db.execute("SELECT * FROM tasks_log WHERE session_id=?", (session_id,)).fetchall()
        discoveries = db.execute("SELECT * FROM discoveries WHERE session_id=?", (session_id,)).fetchall()
        result["tasks"] = [dict(r) for r in tasks]
        result["discoveries"] = [dict(r) for r in discoveries]
        return result
    finally:
        db.close()

@router.post("/sessions")
async def create_session(data: SessionCreate):
    db = get_db()
    try:
        # Auto session_num
        if not data.session_num:
            row = db.execute(
                "SELECT COALESCE(MAX(session_num),0)+1 FROM sessions WHERE device_name=?",
                (data.device_name,)
            ).fetchone()
            data.session_num = row[0]

        # Device lookup
        device = db.execute("SELECT id, platform FROM devices WHERE name=?", (data.device_name,)).fetchone()
        device_id = device[0] if device else None
        platform = device[1] if device else "unknown"

        cur = db.execute("""
            INSERT INTO sessions (session_num, date, summary, tasks_completed, files_changed, bugs_found, notes, device_id, platform, device_name)
            VALUES (?, date('now'), ?, ?, ?, ?, ?, ?, ?, ?)
        """, (data.session_num, data.summary,
              json.dumps(data.tasks_completed) if data.tasks_completed else None,
              json.dumps(data.files_changed) if data.files_changed else None,
              json.dumps(data.bugs_found) if data.bugs_found else None,
              data.notes, device_id, platform, data.device_name))
        db.commit()

        # Update device last_seen
        if device_id:
            db.execute("UPDATE devices SET last_seen=datetime('now') WHERE id=?", (device_id,))
            db.commit()

        return {"id": cur.lastrowid, "session_num": data.session_num, "status": "created"}
    finally:
        db.close()


# ============ Tasks Log ============

@router.get("/tasks")
async def list_tasks(
    project: Optional[str] = None,
    device: Optional[str] = None,
    limit: int = 30
):
    db = get_db()
    try:
        query = "SELECT id, session_id, device_name, project, task, status, date(created_at) as date FROM tasks_log WHERE 1=1"
        params = []
        if project:
            query += " AND project=?"
            params.append(project)
        if device:
            query += " AND device_name=?"
            params.append(device)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()

@router.post("/tasks")
async def create_task_log(data: TaskLogCreate):
    db = get_db()
    try:
        cur = db.execute("""
            INSERT INTO tasks_log (session_id, device_name, project, task, status, files_changed, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (data.session_id, data.device_name, data.project, data.task, data.status,
              json.dumps(data.files_changed) if data.files_changed else None, data.details))
        db.commit()
        return {"id": cur.lastrowid, "status": "created"}
    finally:
        db.close()


# ============ Discoveries ============

@router.get("/discoveries")
async def list_discoveries(
    project: Optional[str] = None,
    type: Optional[str] = None,
    resolved: Optional[int] = None,
    limit: int = 30
):
    db = get_db()
    try:
        query = "SELECT id, session_id, device_name, project, type, title, resolved, date(created_at) as date FROM discoveries WHERE 1=1"
        params = []
        if project:
            query += " AND project=?"
            params.append(project)
        if type:
            query += " AND type=?"
            params.append(type)
        if resolved is not None:
            query += " AND resolved=?"
            params.append(resolved)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()

@router.post("/discoveries")
async def create_discovery(data: DiscoveryCreate):
    db = get_db()
    try:
        cur = db.execute("""
            INSERT INTO discoveries (session_id, device_name, project, type, title, details, resolved)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (data.session_id, data.device_name, data.project, data.type,
              data.title, data.details, data.resolved))
        db.commit()
        return {"id": cur.lastrowid, "status": "created"}
    finally:
        db.close()

@router.put("/discoveries/{discovery_id}/resolve")
async def resolve_discovery(discovery_id: int):
    db = get_db()
    try:
        db.execute("UPDATE discoveries SET resolved=1 WHERE id=?", (discovery_id,))
        db.commit()
        return {"status": "resolved"}
    finally:
        db.close()


# ============ Notes (Cross-Device) ============

@router.get("/notes")
async def list_notes(
    device: Optional[str] = None,
    unread_only: bool = False
):
    db = get_db()
    try:
        query = "SELECT * FROM notes WHERE 1=1"
        params = []
        if device:
            query += " AND (to_device=? OR to_device IS NULL)"
            params.append(device)
        if unread_only:
            query += " AND read=0"
        query += " ORDER BY created_at DESC LIMIT 50"
        rows = db.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()

@router.post("/notes")
async def create_note(data: NoteCreate):
    db = get_db()
    try:
        cur = db.execute("""
            INSERT INTO notes (from_device, to_device, title, content)
            VALUES (?, ?, ?, ?)
        """, (data.from_device, data.to_device, data.title, data.content))
        db.commit()
        return {"id": cur.lastrowid, "status": "created"}
    finally:
        db.close()

@router.put("/notes/{note_id}/read")
async def mark_note_read(note_id: int):
    db = get_db()
    try:
        db.execute("UPDATE notes SET read=1 WHERE id=?", (note_id,))
        db.commit()
        return {"status": "read"}
    finally:
        db.close()


# ============ Device Projects ============

@router.get("/device-projects")
async def list_device_projects(device: Optional[str] = None):
    db = get_db()
    try:
        query = "SELECT * FROM device_projects"
        params = []
        if device:
            query += " WHERE device_name=?"
            params.append(device)
        query += " ORDER BY last_activity DESC"
        rows = db.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()

@router.post("/device-projects")
async def register_device_project(data: DeviceProjectCreate):
    db = get_db()
    try:
        db.execute("""
            INSERT INTO device_projects (device_name, project, local_path)
            VALUES (?, ?, ?)
            ON CONFLICT(device_name, project) DO UPDATE SET
                local_path=excluded.local_path, last_activity=datetime('now')
        """, (data.device_name, data.project, data.local_path))
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()


# ============ Search ============

@router.get("/search")
async def search_all(q: str = Query(..., min_length=2)):
    """Tüm tablolarda full-text arama"""
    db = get_db()
    try:
        pattern = f"%{q}%"
        results = {}

        memories = db.execute(
            "SELECT id, type, name, description FROM memories WHERE active=1 AND (content LIKE ? OR name LIKE ?) LIMIT 10",
            (pattern, pattern)
        ).fetchall()
        results["memories"] = [dict(r) for r in memories]

        sessions = db.execute(
            "SELECT id, session_num, date, device_name, substr(summary,1,100) as summary FROM sessions "
            "WHERE summary LIKE ? OR tasks_completed LIKE ? LIMIT 10",
            (pattern, pattern)
        ).fetchall()
        results["sessions"] = [dict(r) for r in sessions]

        discoveries = db.execute(
            "SELECT id, project, type, title, device_name FROM discoveries "
            "WHERE title LIKE ? OR details LIKE ? LIMIT 10",
            (pattern, pattern)
        ).fetchall()
        results["discoveries"] = [dict(r) for r in discoveries]

        tasks = db.execute(
            "SELECT id, project, task, device_name FROM tasks_log "
            "WHERE task LIKE ? OR details LIKE ? LIMIT 10",
            (pattern, pattern)
        ).fetchall()
        results["tasks"] = [dict(r) for r in tasks]

        return results
    finally:
        db.close()


# ============ Onboarding Prompt ============

@public_router.get("/onboard/{device_name}")
async def get_onboard_prompt(device_name: str):
    """Cihaza özel Claude onboarding prompt'u döner — yapıştır, otomatik çalışsın"""
    db = get_db()
    try:
        device = db.execute("SELECT * FROM devices WHERE name=?", (device_name,)).fetchone()
        if not device:
            raise HTTPException(404, f"Device '{device_name}' not found. Registered devices: " +
                ", ".join(r[0] for r in db.execute("SELECT name FROM devices").fetchall()))
        dev = dict(device)

        # Son oturumlar (tüm cihazlar)
        recent = db.execute(
            "SELECT session_num, date, device_name, substr(summary,1,80) as summary "
            "FROM sessions ORDER BY id DESC LIMIT 5"
        ).fetchall()
        recent_text = "\n".join(f"  - #{r[0]} ({r[2]}, {r[1]}): {r[3]}" for r in recent)

        # Açık bug'lar
        bugs = db.execute(
            "SELECT project, title, device_name FROM discoveries WHERE type='bug' AND resolved=0"
        ).fetchall()
        bugs_text = "\n".join(f"  - [{r[0]}] {r[1]} (bulan: {r[2]})" for r in bugs) if bugs else "  Yok"

        # Okunmamış notlar
        notes = db.execute(
            "SELECT from_device, title, content FROM notes WHERE (to_device=? OR to_device IS NULL) AND read=0",
            (device_name,)
        ).fetchall()
        notes_text = "\n".join(f"  - {r[0]}: {r[1]} — {r[2]}" for r in notes) if notes else "  Yok"

        # Hafızalar
        memories = db.execute(
            "SELECT type, name, content FROM memories WHERE active=1 ORDER BY type"
        ).fetchall()
        mem_text = "\n".join(f"  [{r[0]}] {r[1]}: {r[2][:120]}" for r in memories)

        # Stats
        stats = db.execute("SELECT COUNT(*) FROM sessions WHERE device_name=?", (device_name,)).fetchone()[0]

        API = "http://100.113.153.62:8420/api/v1/memory"
        KEY = MEMORY_API_KEY
        DN = device_name

        prompt = f"""# Merkezi Hafıza Sistemi — {dev['name']} ({dev['platform']})

Sen benim çoklu cihazda çalışan Claude asistanımsın. Klipper sunucumda merkezi bir hafıza sistemi var. Her şeyi buraya kaydet.

## Bağlantı

- **API:** `{API}`
- **Auth Header:** `X-Memory-Key: {KEY}`
- **Cihaz adın:** `{DN}`
- **Platform:** `{dev['platform']}`
- **Bu cihazda önceki oturum sayısı:** {stats}

## Güncel Durum

**Son oturumlar (tüm cihazlar):**
{recent_text}

**Açık bug'lar:**
{bugs_text}

**Okunmamış notlar:**
{notes_text}

## Hafıza
{mem_text}

## Ne Yapmalısın

### Oturum başında (HER ZAMAN):
```bash
curl -s -H "X-Memory-Key: {KEY}" {API}/dashboard
curl -s -H "X-Memory-Key: {KEY}" "{API}/notes?device={DN}&unread_only=true"
```

### Oturum sonunda (HER ZAMAN):
```bash
curl -s -X POST {API}/sessions -H "Content-Type: application/json" -H "X-Memory-Key: {KEY}" -d '{{"device_name":"{DN}","summary":"ÖZET","tasks_completed":["görev1"],"files_changed":["dosya1"],"notes":"notlar"}}'
```

### Bug bulduğunda:
```bash
curl -s -X POST {API}/discoveries -H "Content-Type: application/json" -H "X-Memory-Key: {KEY}" -d '{{"device_name":"{DN}","project":"PROJE","type":"bug","title":"BAŞLIK","details":"DETAY"}}'
```

### Görev tamamladığında:
```bash
curl -s -X POST {API}/tasks -H "Content-Type: application/json" -H "X-Memory-Key: {KEY}" -d '{{"device_name":"{DN}","project":"PROJE","task":"NE YAPILDI","details":"DETAY"}}'
```

### Bug çözdüğünde:
```bash
curl -s -X PUT {API}/discoveries/BUG_ID/resolve -H "X-Memory-Key: {KEY}"
```

### Diğer cihazlara not bırakmak için:
```bash
curl -s -X POST {API}/notes -H "Content-Type: application/json" -H "X-Memory-Key: {KEY}" -d '{{"from_device":"{DN}","title":"BAŞLIK","content":"İÇERİK"}}'
```

### Hafızaya kaydetmek için:
```bash
curl -s -X POST {API}/memories -H "Content-Type: application/json" -H "X-Memory-Key: {KEY}" -d '{{"type":"TYPE","name":"İSİM","description":"AÇIKLAMA","content":"İÇERİK","source_device":"{DN}"}}'
```
Type: user, feedback, project, reference

### Arama:
```bash
curl -s -H "X-Memory-Key: {KEY}" "{API}/search?q=KELIME"
```

## Projeler

| Proje | URL | Stack |
|-------|-----|-------|
| Linux-AI Server | 100.113.153.62:8420 | FastAPI, SQLite, kernel |
| PetVet | petvet.panola.app | React 19, CF Workers+D1 |
| Kuafor SaaS | kuafor.panola.app | React 19, CF Workers+D1 |
| Panola ERP | panola.app | React 19, Supabase |
| BilgeArena | bilgearena.com | Next.js |
| Renderhane | renderhane.com | Next.js, Supabase, fal.ai |

## Kurallar

- Türkçe konuş
- Gereksiz açıklama yapma, direkt çöz
- Onay beklemeden çalış
- Her oturum sonunda MUTLAKA session kaydet
- Bug bulursan HEMEN discovery olarak kaydet
- Commit mesajlarına Co-Authored-By EKLEME (Vercel engelliyor)
- Renderhane git push: author turer73 olmalı (KlipperOS commit Vercel deploy etmez)

## Şimdi

Önce dashboard'u kontrol et, okunmamış notları oku, sonra bana nasıl yardımcı olabileceğini sor.
"""
        return {"device": device_name, "platform": dev["platform"], "prompt": prompt}
    finally:
        db.close()


@public_router.get("/onboard/{device_name}/raw")
async def get_onboard_prompt_raw(device_name: str):
    """Düz metin prompt — doğrudan kopyala-yapıştır"""
    from fastapi.responses import PlainTextResponse
    result = await get_onboard_prompt(device_name)
    return PlainTextResponse(result["prompt"])
