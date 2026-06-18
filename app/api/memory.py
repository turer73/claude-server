"""
Claude Memory API v2 — Merkezi hafıza sistemi
Duplicate koruması, FTS arama, read tracking, lifecycle yönetimi.
"""
import os
import sqlite3
import json
import re
import asyncio
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, Header
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, field_validator
from typing import Optional, Literal
import httpx

DB_PATH = "/opt/linux-ai-server/data/claude_memory.db"

MEMORY_API_KEY = os.environ.get("MEMORY_API_KEY", "")
if not MEMORY_API_KEY:
    _env_path = "/opt/linux-ai-server/.env"
    if os.path.exists(_env_path):
        with open(_env_path) as _f:
            for _line in _f:
                if _line.startswith("MEMORY_API_KEY="):
                    MEMORY_API_KEY = _line.strip().split("=", 1)[1]
                    break

VALID_DISCOVERY_TYPES = ("bug", "fix", "learning", "config", "workaround", "architecture", "plan")
VALID_STATUSES = ("active", "completed", "obsolete", "superseded")
TRASH_TITLES = re.compile(r"^(test|test bug|test fix|test workaround|deneme|asdf|xxx)$", re.IGNORECASE)


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


def _track_read(db, table: str, row_id: int):
    """Read tracking — her okumada sayaç artır"""
    db.execute(f"UPDATE {table} SET read_count=read_count+1, last_read_at=datetime('now') WHERE id=?", (row_id,))
    db.commit()


def _sync_fts(db, disc_id: int, title: str, details: str = ""):
    """FTS index güncelle"""
    try:
        db.execute("INSERT INTO discoveries_fts(rowid, title, details) VALUES (?, ?, ?)",
                   (disc_id, title, details or ""))
    except Exception:
        pass


# ============ Webhook / Event System ============

_WEBHOOK_TIMEOUT = 5

_TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

def _ensure_webhooks_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS webhooks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event TEXT NOT NULL,
        url TEXT NOT NULL,
        secret TEXT DEFAULT '',
        active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_webhooks_event ON webhooks(event)""")
    db.commit()

async def _send_telegram(message: str, parse_mode: str = "HTML"):
    """Fire-and-forget Telegram bildirimi"""
    if not _TELEGRAM_BOT_TOKEN or not _TELEGRAM_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"https://api.telegram.org/bot{_TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": _TELEGRAM_CHAT_ID,
                    "text": message,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
            )
    except Exception:
        pass

async def _fire_event(event: str, payload: dict):
    """Event webhook'larını async fire-and-forget fırlat + Telegram bildirimi"""
    try:
        db = get_db()
        _ensure_webhooks_table(db)
        hooks = db.execute(
            "SELECT url, secret FROM webhooks WHERE event=? AND active=1",
            (event,)
        ).fetchall()
        db.close()

        # Webhook HTTP POST
        if hooks:
            async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT) as client:
                tasks = []
                for url, secret in hooks:
                    headers = {"Content-Type": "application/json"}
                    if secret:
                        headers["X-Webhook-Secret"] = secret
                    tasks.append(client.post(url, json=payload, headers=headers))
                await asyncio.gather(*tasks, return_exceptions=True)

        # Telegram bildirimi (kritik olaylar)
        if event in ("bug_created", "fix_created"):
            emoji = "🐛" if event == "bug_created" else "🔧"
            msg = (
                f"<b>{emoji} Yeni {event.split('_')[0]}!</b>\n"
                f"Proje: <code>{payload.get('project', '?')}</code>\n"
                f"Başlık: {payload.get('title', '?')[:200]}"
            )
            await _send_telegram(msg)
        elif event == "task_created":
            msg = (
                f"<b>📋 Yeni Task</b>\n"
                f"Proje: <code>{payload.get('project', '?')}</code>\n"
                f"Task: {payload.get('task', '?')[:200]}"
            )
            await _send_telegram(msg)
        elif event == "note_created":
            msg = (
                f"<b>📝 Yeni Not</b>\n"
                f"Gönderen: <code>{payload.get('from_device', '?')}</code>\n"
                f"{payload.get('title', '?')[:200]}"
            )
            await _send_telegram(msg)
    except Exception:
        pass


# ============ Context Helpers ============

_TOKEN_BUDGET = 2000

def _estimate_tokens(text: str) -> int:
    """Kaba token tahmini: ~4 karakter = 1 token (Türkçe/İngilizce)"""
    return len(text) // 4 + 1

def _truncate_context(items: list[tuple[str, str, int]], budget: int = _TOKEN_BUDGET) -> str:
    """Greedy context build — en yüksek priority'den başla, budget dolana kadar ekle.
    items: list of (label, content, priority_score)
    """
    parts = []
    used = 0
    sorted_items = sorted(items, key=lambda x: -x[2])
    for label, content, _score in sorted_items:
        t = _estimate_tokens(content)
        if used + t > budget:
            remaining = budget - used
            chars = remaining * 4
            parts.append(f"## {label}\n{content[:chars]}...")
            break
        parts.append(f"## {label}\n{content}")
        used += t
    return "\n\n".join(parts)


# ============ Models ============

class DeviceRegister(BaseModel):
    name: str
    platform: str
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
    type: Literal["user", "feedback", "project", "reference"]
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
    type: str
    title: str
    details: Optional[str] = None
    status: Optional[str] = "active"

    @field_validator("type")
    @classmethod
    def valid_type(cls, v):
        if v not in VALID_DISCOVERY_TYPES:
            raise ValueError(f"Geçersiz tip: {v}. Geçerli: {', '.join(VALID_DISCOVERY_TYPES)}")
        return v

    @field_validator("title")
    @classmethod
    def clean_title(cls, v):
        v = v.strip()
        if len(v) < 3:
            raise ValueError("Title en az 3 karakter olmalı")
        if TRASH_TITLES.match(v):
            raise ValueError(f"'{v}' test/çöp verisi — kaydetmiyorum")
        return v

class DiscoveryUpdate(BaseModel):
    title: Optional[str] = None
    details: Optional[str] = None
    status: Optional[str] = None

    @field_validator("status")
    @classmethod
    def valid_status(cls, v):
        if v and v not in VALID_STATUSES:
            raise ValueError(f"Geçersiz status: {v}. Geçerli: {', '.join(VALID_STATUSES)}")
        return v

class NoteCreate(BaseModel):
    from_device: str
    to_device: Optional[str] = None
    title: str
    content: str

class DeviceProjectCreate(BaseModel):
    device_name: str
    project: str
    local_path: Optional[str] = None


# ============ Dashboard ============

@router.get("/dashboard")
async def memory_dashboard():
    """Akıllı dashboard — stale detection, proje health, action items"""
    db = get_db()
    try:
        stats = {
            "memories": db.execute("SELECT COUNT(*) FROM memories WHERE active=1").fetchone()[0],
            "sessions": db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            "tasks": db.execute("SELECT COUNT(*) FROM tasks_log").fetchone()[0],
            "discoveries": db.execute("SELECT COUNT(*) FROM discoveries").fetchone()[0],
            "open_bugs": db.execute("SELECT COUNT(*) FROM discoveries WHERE type='bug' AND status='active'").fetchone()[0],
            "architecture": db.execute("SELECT COUNT(*) FROM discoveries WHERE type='architecture' AND status='active'").fetchone()[0],
            "active_plans": db.execute("SELECT COUNT(*) FROM discoveries WHERE type='plan' AND status='active'").fetchone()[0],
            "completed_plans": db.execute("SELECT COUNT(*) FROM discoveries WHERE type='plan' AND status='completed'").fetchone()[0],
            "fixes": db.execute("SELECT COUNT(*) FROM discoveries WHERE type='fix'").fetchone()[0],
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
            "SELECT id, project, title, device_name, created_at FROM discoveries "
            "WHERE type='bug' AND status='active' ORDER BY created_at DESC"
        ).fetchall()]

        # Stale data — 60+ gün okunamayan active kayıtlar
        stale = [dict(r) for r in db.execute(
            "SELECT id, project, type, title, date(created_at) as created, read_count "
            "FROM discoveries WHERE status='active' AND read_count=0 "
            "AND created_at < datetime('now', '-60 days') ORDER BY created_at LIMIT 10"
        ).fetchall()]

        # Hiç okunmamış kayıt sayısı
        never_read = db.execute(
            "SELECT COUNT(*) FROM discoveries WHERE read_count=0"
        ).fetchone()[0]

        # Proje bazlı özet
        projects = [dict(r) for r in db.execute(
            "SELECT project, COUNT(*) as total, "
            "SUM(CASE WHEN type='bug' AND status='active' THEN 1 ELSE 0 END) as open_bugs, "
            "SUM(CASE WHEN type='architecture' THEN 1 ELSE 0 END) as arch, "
            "SUM(CASE WHEN type='plan' AND status='active' THEN 1 ELSE 0 END) as active_plans "
            "FROM discoveries GROUP BY project ORDER BY total DESC"
        ).fetchall()]

        return {
            "stats": stats,
            "devices": devices,
            "recent_sessions": recent_sessions,
            "open_bugs": open_bugs,
            "stale_data": stale,
            "never_read_count": never_read,
            "projects": projects,
        }
    finally:
        db.close()


# ============ Devices ============

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
        return {"status": "ok"}
    finally:
        db.close()


# ============ Memories ============

@router.get("/memories")
async def list_memories(type: Optional[str] = None, active: int = 1, search: Optional[str] = None):
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
    db = get_db()
    try:
        # Duplicate kontrolü
        existing = db.execute(
            "SELECT id FROM memories WHERE active=1 AND type=? AND name=?",
            (data.type, data.name)
        ).fetchone()
        if existing:
            # Var olanı güncelle
            db.execute(
                "UPDATE memories SET description=?, content=?, source_device=?, updated_at=datetime('now') WHERE id=?",
                (data.description, data.content, data.source_device, existing[0])
            )
            db.commit()
            return {"id": existing[0], "status": "updated_existing"}

        cur = db.execute(
            "INSERT INTO memories (type, name, description, content, source_device) VALUES (?, ?, ?, ?, ?)",
            (data.type, data.name, data.description, data.content, data.source_device))
        db.commit()
        return {"id": cur.lastrowid, "status": "created"}
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


# ============ Sessions ============

@router.get("/sessions")
async def list_sessions(device: Optional[str] = None, platform: Optional[str] = None, limit: int = 20):
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
        return [dict(r) for r in db.execute(query, params).fetchall()]
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
        result["tasks"] = [dict(r) for r in db.execute("SELECT * FROM tasks_log WHERE session_id=?", (session_id,)).fetchall()]
        result["discoveries"] = [dict(r) for r in db.execute("SELECT * FROM discoveries WHERE session_id=?", (session_id,)).fetchall()]
        return result
    finally:
        db.close()

@router.post("/sessions")
async def create_session(data: SessionCreate):
    db = get_db()
    try:
        if not data.session_num:
            row = db.execute("SELECT COALESCE(MAX(session_num),0)+1 FROM sessions WHERE device_name=?", (data.device_name,)).fetchone()
            data.session_num = row[0]

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

        if device_id:
            db.execute("UPDATE devices SET last_seen=datetime('now') WHERE id=?", (device_id,))
            db.commit()

        return {"id": cur.lastrowid, "session_num": data.session_num, "status": "created"}
    finally:
        db.close()

@router.post("/sessions/auto-create")
async def auto_create_session(data: SessionCreate):
    """Session oluştur + bugs_found/tasks_completed'dan otomatik discovery/task çıkar"""
    db = get_db()
    try:
        if not data.session_num:
            row = db.execute("SELECT COALESCE(MAX(session_num),0)+1 FROM sessions WHERE device_name=?", (data.device_name,)).fetchone()
            data.session_num = row[0]

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
        session_id = cur.lastrowid

        if device_id:
            db.execute("UPDATE devices SET last_seen=datetime('now') WHERE id=?", (device_id,))
            db.commit()

        # Auto-çıkarım: bugs_found → discovery
        discoveries_created = 0
        if data.bugs_found:
            for bug in data.bugs_found:
                try:
                    existing = db.execute(
                        "SELECT id FROM discoveries WHERE project=? AND type='bug' AND title=?",
                        ("bilge-arena", bug[:200])
                    ).fetchone()
                    if not existing:
                        db.execute(
                            "INSERT INTO discoveries (session_id, device_name, project, type, title, details, status) "
                            "VALUES (?, ?, ?, 'bug', ?, ?, 'active')",
                            (session_id, data.device_name, "bilge-arena", bug[:200], data.summary[:500])
                        )
                        discoveries_created += 1
                except Exception:
                    pass

        # Auto-çıkarım: tasks_completed → task
        tasks_created = 0
        if data.tasks_completed:
            for task in data.tasks_completed:
                try:
                    existing = db.execute(
                        "SELECT id FROM tasks_log WHERE project=? AND task=?",
                        ("bilge-arena", task[:200])
                    ).fetchone()
                    if not existing:
                        db.execute(
                            "INSERT INTO tasks_log (session_id, device_name, project, task, status, details) "
                            "VALUES (?, ?, ?, ?, 'completed', ?)",
                            (session_id, data.device_name, "bilge-arena", task[:200], data.summary[:500])
                        )
                        tasks_created += 1
                except Exception:
                    pass

        db.commit()

        asyncio.create_task(_fire_event("session_created", {
            "session_id": session_id, "device": data.device_name,
            "discoveries": discoveries_created, "tasks": tasks_created,
        }))

        return {
            "id": session_id, "session_num": data.session_num, "status": "created",
            "auto_discoveries": discoveries_created, "auto_tasks": tasks_created,
        }
    finally:
        db.close()


# ============ Tasks Log ============

@router.get("/tasks")
async def list_tasks(project: Optional[str] = None, device: Optional[str] = None, limit: int = 30):
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
        return [dict(r) for r in db.execute(query, params).fetchall()]
    finally:
        db.close()

@router.post("/tasks")
async def create_task_log(data: TaskLogCreate):
    db = get_db()
    try:
        # Duplicate kontrolü — aynı proje + aynı task adı
        existing = db.execute(
            "SELECT id FROM tasks_log WHERE project=? AND task=?",
            (data.project, data.task)
        ).fetchone()
        if existing:
            return {"id": existing[0], "status": "already_exists"}

        cur = db.execute("""
            INSERT INTO tasks_log (session_id, device_name, project, task, status, files_changed, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (data.session_id, data.device_name, data.project, data.task, data.status,
              json.dumps(data.files_changed) if data.files_changed else None, data.details))
        db.commit()
        new_id = cur.lastrowid

        asyncio.create_task(_fire_event("task_created", {
            "id": new_id, "project": data.project, "task": data.task,
            "status": data.status, "device": data.device_name,
        }))

        return {"id": new_id, "status": "created"}
    finally:
        db.close()


# ============ Discoveries ============

@router.get("/discoveries")
async def list_discoveries(
    project: Optional[str] = None,
    type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 30
):
    db = get_db()
    try:
        query = ("SELECT id, session_id, device_name, project, type, title, status, "
                 "read_count, date(created_at) as date FROM discoveries WHERE 1=1")
        params = []
        if project:
            query += " AND project=?"
            params.append(project)
        if type:
            query += " AND type=?"
            params.append(type)
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in db.execute(query, params).fetchall()]
    finally:
        db.close()

@router.get("/discoveries/{discovery_id}")
async def get_discovery(discovery_id: int):
    db = get_db()
    try:
        row = db.execute("SELECT * FROM discoveries WHERE id=?", (discovery_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Discovery not found")
        _track_read(db, "discoveries", discovery_id)
        return dict(row)
    finally:
        db.close()

@router.post("/discoveries")
async def create_discovery(data: DiscoveryCreate):
    """Duplicate korumalı discovery oluştur — aynı project+type+title varsa günceller"""
    db = get_db()
    try:
        existing = db.execute(
            "SELECT id FROM discoveries WHERE project=? AND type=? AND title=?",
            (data.project, data.type, data.title)
        ).fetchone()
        if existing:
            # Var olanı güncelle (details değiştiyse)
            if data.details:
                db.execute("UPDATE discoveries SET details=?, device_name=? WHERE id=?",
                           (data.details, data.device_name, existing[0]))
                db.commit()
            return {"id": existing[0], "status": "already_exists"}

        cur = db.execute("""
            INSERT INTO discoveries (session_id, device_name, project, type, title, details, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (data.session_id, data.device_name, data.project, data.type,
              data.title, data.details, data.status or "active"))
        db.commit()
        _sync_fts(db, cur.lastrowid, data.title, data.details)
        db.commit()
        new_id = cur.lastrowid

        # Event trigger
        event_type = f"{data.type}_created" if data.type in ("bug", "fix") else "discovery_created"
        asyncio.create_task(_fire_event(event_type, {
            "id": new_id, "project": data.project, "type": data.type,
            "title": data.title, "device": data.device_name,
        }))

        return {"id": new_id, "status": "created"}
    finally:
        db.close()

@router.put("/discoveries/{discovery_id}")
async def update_discovery(discovery_id: int, data: DiscoveryUpdate):
    """Discovery güncelle — status lifecycle (active → completed/obsolete/superseded)"""
    db = get_db()
    try:
        fields, params = [], []
        if data.title:
            fields.append("title=?")
            params.append(data.title)
        if data.details:
            fields.append("details=?")
            params.append(data.details)
        if data.status:
            fields.append("status=?")
            params.append(data.status)
            if data.status == "completed":
                fields.append("resolved=1")
        if not fields:
            raise HTTPException(400, "No fields to update")
        params.append(discovery_id)
        db.execute(f"UPDATE discoveries SET {', '.join(fields)} WHERE id=?", params)
        db.commit()
        return {"status": "updated"}
    finally:
        db.close()

@router.put("/discoveries/{discovery_id}/resolve")
async def resolve_discovery(discovery_id: int):
    db = get_db()
    try:
        db.execute("UPDATE discoveries SET resolved=1, status='completed' WHERE id=?", (discovery_id,))
        db.commit()
        return {"status": "resolved"}
    finally:
        db.close()

@router.get("/discoveries/by-type/{dtype}")
async def list_discoveries_by_type(dtype: str, project: Optional[str] = None, status: Optional[str] = "active"):
    db = get_db()
    try:
        query = ("SELECT id, project, type, title, details, status, read_count, "
                 "device_name, date(created_at) as date FROM discoveries WHERE type=?")
        params = [dtype]
        if project:
            query += " AND project=?"
            params.append(project)
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY project, created_at DESC"
        rows = db.execute(query, params).fetchall()
        # Toplu read tracking
        for r in rows:
            _track_read(db, "discoveries", r["id"])
        return [dict(r) for r in rows]
    finally:
        db.close()


# ============ Projects ============

@router.get("/projects")
async def list_projects_summary():
    """Proje bazlı özet — health skoru ile"""
    db = get_db()
    try:
        projects = {}
        for row in db.execute("""
            SELECT project, type, status, COUNT(*) as cnt
            FROM discoveries GROUP BY project, type, status ORDER BY project
        """).fetchall():
            p = row[0]
            if p not in projects:
                projects[p] = {"name": p, "open_bugs": 0, "fixes": 0, "architecture": 0,
                               "active_plans": 0, "completed_plans": 0, "workarounds": 0, "tasks": 0}
            t, s, c = row[1], row[2], row[3]
            if t == "bug" and s == "active":
                projects[p]["open_bugs"] = c
            elif t == "fix":
                projects[p]["fixes"] += c
            elif t == "architecture":
                projects[p]["architecture"] += c
            elif t == "plan" and s == "active":
                projects[p]["active_plans"] = c
            elif t == "plan" and s == "completed":
                projects[p]["completed_plans"] = c
            elif t == "workaround":
                projects[p]["workarounds"] += c

        for row in db.execute("SELECT project, COUNT(*) FROM tasks_log GROUP BY project").fetchall():
            if row[0] in projects:
                projects[row[0]]["tasks"] = row[1]
            else:
                projects[row[0]] = {"name": row[0], "open_bugs": 0, "fixes": 0, "architecture": 0,
                                     "active_plans": 0, "completed_plans": 0, "workarounds": 0, "tasks": row[1]}

        # Health skoru: mimari var, plan var, bug az = sağlıklı
        for p in projects.values():
            score = 0
            if p["architecture"] > 0: score += 30
            if p["active_plans"] > 0 or p["completed_plans"] > 0: score += 20
            if p["tasks"] > 0: score += 20
            if p["fixes"] > 0: score += 15
            if p["open_bugs"] == 0: score += 15
            elif p["open_bugs"] <= 2: score += 5
            p["health"] = min(score, 100)

        return sorted(projects.values(), key=lambda x: x["health"], reverse=True)
    finally:
        db.close()

@router.get("/projects/{project_name}")
async def get_project_detail(project_name: str):
    """Proje detayı — discoveries, tasks, sessions, health"""
    db = get_db()
    try:
        discoveries = [dict(r) for r in db.execute(
            "SELECT id, type, title, details, status, read_count, device_name, date(created_at) as date "
            "FROM discoveries WHERE project=? ORDER BY type, created_at DESC",
            (project_name,)
        ).fetchall()]

        tasks = [dict(r) for r in db.execute(
            "SELECT id, task, status, device_name, details, date(created_at) as date "
            "FROM tasks_log WHERE project=? ORDER BY created_at DESC LIMIT 30",
            (project_name,)
        ).fetchall()]

        sessions = [dict(r) for r in db.execute(
            "SELECT id, session_num, date, device_name, platform, substr(summary,1,120) as summary "
            "FROM sessions WHERE summary LIKE ? ORDER BY id DESC LIMIT 10",
            (f"%{project_name}%",)
        ).fetchall()]

        devices = [dict(r) for r in db.execute(
            "SELECT device_name, local_path, datetime(last_activity) as last_activity "
            "FROM device_projects WHERE project=?",
            (project_name,)
        ).fetchall()]

        type_counts = {}
        for d in discoveries:
            key = f"{d['type']}_{d['status']}" if d['status'] != 'active' else d['type']
            type_counts[key] = type_counts.get(key, 0) + 1

        return {
            "project": project_name,
            "stats": type_counts,
            "total_discoveries": len(discoveries),
            "total_tasks": len(tasks),
            "discoveries": discoveries,
            "tasks": tasks,
            "sessions": sessions,
            "devices": devices,
        }
    finally:
        db.close()


# ============ Notes ============

@router.get("/notes")
async def list_notes(device: Optional[str] = None, unread_only: bool = False):
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
        return [dict(r) for r in db.execute(query, params).fetchall()]
    finally:
        db.close()

@router.post("/notes")
async def create_note(data: NoteCreate):
    db = get_db()
    try:
        cur = db.execute("INSERT INTO notes (from_device, to_device, title, content) VALUES (?, ?, ?, ?)",
                         (data.from_device, data.to_device, data.title, data.content))
        db.commit()
        new_id = cur.lastrowid

        asyncio.create_task(_fire_event("note_created", {
            "id": new_id, "from_device": data.from_device,
            "to_device": data.to_device, "title": data.title,
        }))

        return {"id": new_id, "status": "created"}
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
        return [dict(r) for r in db.execute(query, params).fetchall()]
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


# ============ Webhooks (Event-driven) ============

@router.get("/webhooks")
async def list_webhooks():
    db = get_db()
    try:
        _ensure_webhooks_table(db)
        return [dict(r) for r in db.execute("SELECT * FROM webhooks ORDER BY event, id").fetchall()]
    finally:
        db.close()

@router.post("/webhooks")
async def register_webhook(event: str, url: str, secret: str = ""):
    """Event webhook'u kaydet. Event: bug_created, task_created, note_created, all"""
    db = get_db()
    try:
        _ensure_webhooks_table(db)
        existing = db.execute(
            "SELECT id FROM webhooks WHERE event=? AND url=?", (event, url)
        ).fetchone()
        if existing:
            return {"id": existing[0], "status": "already_exists"}
        cur = db.execute(
            "INSERT INTO webhooks (event, url, secret) VALUES (?, ?, ?)",
            (event, url, secret)
        )
        db.commit()
        return {"id": cur.lastrowid, "status": "created"}
    finally:
        db.close()

@router.delete("/webhooks/{webhook_id}")
async def delete_webhook(webhook_id: int):
    db = get_db()
    try:
        _ensure_webhooks_table(db)
        db.execute("DELETE FROM webhooks WHERE id=?", (webhook_id,))
        db.commit()
        return {"status": "deleted"}
    finally:
        db.close()

@router.get("/webhooks/telegram-status")
async def telegram_status():
    """Telegram bot bağlantı durumu"""
    if not _TELEGRAM_BOT_TOKEN:
        return {"configured": False, "message": "TELEGRAM_BOT_TOKEN env değişkeni eksik"}
    if not _TELEGRAM_CHAT_ID:
        return {"configured": False, "message": "TELEGRAM_CHAT_ID env değişkeni eksik"}
    # Test mesajı gönder
    await _send_telegram("✅ <b>Klipper Hafıza Sistemi</b>\nTelegram bildirimleri aktif!")
    return {"configured": True, "message": "Test mesajı gönderildi"}


# ============ Search (FTS) ============

@router.get("/search")
async def search_all(q: str = Query(..., min_length=2)):
    """FTS + LIKE hibrit arama"""
    db = get_db()
    try:
        results = {}

        # FTS arama (discoveries)
        try:
            fts_rows = db.execute(
                "SELECT rowid, highlight(discoveries_fts, 0, '**', '**') as title, "
                "highlight(discoveries_fts, 1, '**', '**') as details "
                "FROM discoveries_fts WHERE discoveries_fts MATCH ? LIMIT 15",
                (q,)
            ).fetchall()
            fts_ids = [r[0] for r in fts_rows]
            if fts_ids:
                placeholders = ",".join("?" * len(fts_ids))
                results["discoveries"] = [dict(r) for r in db.execute(
                    f"SELECT id, project, type, title, status, device_name, date(created_at) as date "
                    f"FROM discoveries WHERE id IN ({placeholders})", fts_ids
                ).fetchall()]
            else:
                results["discoveries"] = []
        except Exception:
            # FTS fallback → LIKE
            pattern = f"%{q}%"
            results["discoveries"] = [dict(r) for r in db.execute(
                "SELECT id, project, type, title, status, device_name FROM discoveries "
                "WHERE title LIKE ? OR details LIKE ? LIMIT 15", (pattern, pattern)
            ).fetchall()]

        # Memories — LIKE
        pattern = f"%{q}%"
        results["memories"] = [dict(r) for r in db.execute(
            "SELECT id, type, name, description FROM memories WHERE active=1 AND (content LIKE ? OR name LIKE ?) LIMIT 10",
            (pattern, pattern)
        ).fetchall()]

        # Sessions — LIKE
        results["sessions"] = [dict(r) for r in db.execute(
            "SELECT id, session_num, date, device_name, substr(summary,1,100) as summary FROM sessions "
            "WHERE summary LIKE ? OR tasks_completed LIKE ? LIMIT 10",
            (pattern, pattern)
        ).fetchall()]

        # Tasks — LIKE
        results["tasks"] = [dict(r) for r in db.execute(
            "SELECT id, project, task, device_name FROM tasks_log WHERE task LIKE ? OR details LIKE ? LIMIT 10",
            (pattern, pattern)
        ).fetchall()]

        total = sum(len(v) for v in results.values())
        return {"query": q, "total": total, "results": results}
    finally:
        db.close()


# ============ Health & Maintenance ============

@router.get("/health")
async def memory_health():
    """Sistem sağlık raporu — stale data, never-read, duplicates"""
    db = get_db()
    try:
        total = db.execute("SELECT COUNT(*) FROM discoveries").fetchone()[0]
        never_read = db.execute("SELECT COUNT(*) FROM discoveries WHERE read_count=0").fetchone()[0]
        stale_60 = db.execute(
            "SELECT COUNT(*) FROM discoveries WHERE status='active' AND created_at < datetime('now', '-60 days')"
        ).fetchone()[0]
        most_read = [dict(r) for r in db.execute(
            "SELECT id, project, type, title, read_count FROM discoveries ORDER BY read_count DESC LIMIT 5"
        ).fetchall()]

        return {
            "total_discoveries": total,
            "never_read": never_read,
            "never_read_pct": round(never_read / total * 100, 1) if total > 0 else 0,
            "stale_60_days": stale_60,
            "most_read": most_read,
            "recommendation": "Sistem sağlıklı" if never_read / max(total, 1) < 0.5 else "Çok fazla okunmayan kayıt — temizlik gerekiyor",
        }
    finally:
        db.close()

@router.post("/maintenance/archive-stale")
async def archive_stale(days: int = 90):
    """Eski, hiç okunmamış kayıtları obsolete yap"""
    db = get_db()
    try:
        cur = db.execute(
            "UPDATE discoveries SET status='obsolete' "
            "WHERE status='active' AND read_count=0 AND type NOT IN ('bug') "
            "AND created_at < datetime('now', ? || ' days')",
            (f"-{days}",)
        )
        db.commit()
        return {"archived": cur.rowcount}
    finally:
        db.close()

@router.post("/maintenance/auto-cleanup")
async def auto_cleanup(days: int = 60, dry_run: bool = False):
    """Kapsamlı bakım — stale arşivle + FTS temizlik + rapor"""
    db = get_db()
    try:
        # 1. Stale archive
        stale_count = 0
        if not dry_run:
            cur = db.execute(
                "UPDATE discoveries SET status='obsolete' "
                "WHERE status='active' AND read_count=0 AND type NOT IN ('bug') "
                "AND created_at < datetime('now', ? || ' days')",
                (f"-{days}",)
            )
            stale_count = cur.rowcount
        else:
            stale_count = db.execute(
                "SELECT COUNT(*) FROM discoveries WHERE status='active' AND read_count=0 "
                "AND type NOT IN ('bug') AND created_at < datetime('now', ? || ' days')",
                (f"-{days}",)
            ).fetchone()[0]

        # 2. FTS orphan cleanup
        fts_orphans = 0
        if not dry_run:
            db.execute(
                "DELETE FROM discoveries_fts WHERE rowid NOT IN (SELECT id FROM discoveries)"
            )
            db.commit()
            fts_orphans = db.total_changes  # approximate

        # 3. Stats
        total = db.execute("SELECT COUNT(*) FROM discoveries").fetchone()[0]
        never_read = db.execute("SELECT COUNT(*) FROM discoveries WHERE read_count=0").fetchone()[0]
        active_bugs = db.execute("SELECT COUNT(*) FROM discoveries WHERE type='bug' AND status='active'").fetchone()[0]

        report = {
            "action": "dry_run" if dry_run else "cleanup",
            "stale_archived": stale_count,
            "fts_orphans_removed": fts_orphans if not dry_run else "N/A",
            "total_discoveries": total,
            "never_read": never_read,
            "active_bugs": active_bugs,
            "never_read_pct": round(never_read / max(total, 1) * 100, 1),
        }

        # 4. Telegram rapor (dry_run değilse)
        if not dry_run:
            await _send_telegram(
                f"<b>🧹 Klipper Bakım Raporu</b>\n"
                f"Arşivlenen: {stale_count} kayıt\n"
                f"FTS temizlik: {fts_orphans} orphan\n"
                f"Kalan: {total} discovery, {active_bugs} aktif bug\n"
                f"Okunmamış: {never_read} (%{report['never_read_pct']})"
            )

        return report
    finally:
        db.close()

@router.get("/maintenance/detect-conflicts")
async def detect_conflicts():
    """Çakışmaları ve "COZULDU" etiketi olup hala active olan bug'ları tespit et"""
    db = get_db()
    try:
        # Title'ında "COZULDU" geçen ama hala active olan bug'lar
        stale_bugs = [dict(r) for r in db.execute(
            "SELECT id, project, title, status FROM discoveries "
            "WHERE type='bug' AND status='active' AND title LIKE '%COZULDU%' "
            "ORDER BY project"
        ).fetchall()]

        # Aynı project+type+title'dan birden fazla varsa (duplicate)
        dups = [dict(r) for r in db.execute(
            "SELECT project, type, title, COUNT(*) as cnt, "
            "GROUP_CONCAT(id) as ids FROM discoveries "
            "GROUP BY project, type, title HAVING cnt > 1 "
            "ORDER BY cnt DESC LIMIT 20"
        ).fetchall()]

        # Bug'ı fix'lenmiş (benzer başlık) ama hala active
        bug_fix_pairs = []
        bugs = db.execute(
            "SELECT id, project, title FROM discoveries WHERE type='bug' AND status='active'"
        ).fetchall()
        for bug in bugs:
            related = db.execute(
                "SELECT id, title, status FROM discoveries "
                "WHERE type='fix' AND project=? AND title LIKE ? AND status='completed'",
                (bug["project"], f"%{bug['title'][:30]}%")
            ).fetchall()
            if related:
                bug_fix_pairs.append({
                    "bug_id": bug["id"], "bug_title": bug["title"],
                    "fix_id": related[0]["id"], "fix_title": related[0]["title"],
                })

        return {
            "stale_bugs_cozuldu": [dict(r) for r in stale_bugs],
            "duplicate_discoveries": dups,
            "bug_fix_resolvable": bug_fix_pairs[:10],
            "total_stale": len(stale_bugs),
            "total_dups": len(dups),
        }
    finally:
        db.close()


# ============ Onboarding & Context ============

_RAG_BASE = "http://localhost:8420/api/v1/rag"

async def _fetch_rag_context(project: str, top_k: int = 3) -> str:
    """RAG'den proje bağlamı al — hata olursa sessizce geç"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{_RAG_BASE}/search",
                json={"q": f"{project} nedir ne durumda", "top_k": top_k, "project": project},
            )
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", []) if "results" in data else data
                if results and isinstance(results, list):
                    return "\n".join(
                        f"  - {r.get('text', str(r))[:200]}"
                        for r in results[:top_k] if r.get('text')
                    )
    except Exception:
        pass
    return ""

@public_router.get("/onboard/{device_name}")
async def get_onboard_prompt(device_name: str):
    db = get_db()
    try:
        device = db.execute("SELECT * FROM devices WHERE name=?", (device_name,)).fetchone()
        if not device:
            raise HTTPException(404, f"Device '{device_name}' not found")
        dev = dict(device)

        recent = db.execute(
            "SELECT session_num, date, device_name, substr(summary,1,80) as summary "
            "FROM sessions ORDER BY id DESC LIMIT 5"
        ).fetchall()
        recent_text = "\n".join(f"  - #{r[0]} ({r[2]}, {r[1]}): {r[3]}" for r in recent)

        bugs = db.execute(
            "SELECT project, title, device_name FROM discoveries WHERE type='bug' AND status='active'"
        ).fetchall()
        bugs_text = "\n".join(f"  - [{r[0]}] {r[1]} (bulan: {r[2]})" for r in bugs) if bugs else "  Yok"

        notes = db.execute(
            "SELECT from_device, title, content FROM notes WHERE (to_device=? OR to_device IS NULL) AND read=0",
            (device_name,)
        ).fetchall()
        notes_text = "\n".join(f"  - {r[0]}: {r[1]} — {r[2]}" for r in notes) if notes else "  Yok"

        memories = db.execute("SELECT type, name, content FROM memories WHERE active=1 ORDER BY type").fetchall()
        mem_text = "\n".join(f"  [{r[0]}] {r[1]}: {r[2][:120]}" for r in memories)

        stats = db.execute("SELECT COUNT(*) FROM sessions WHERE device_name=?", (device_name,)).fetchone()[0]

        projects = list(set(r[0] for r in db.execute(
            "SELECT project FROM discoveries WHERE status='active' ORDER BY created_at DESC LIMIT 20"
        ).fetchall()))
        rag_contexts = {}
        for p in projects[:5]:
            ctx = await _fetch_rag_context(p)
            if ctx:
                rag_contexts[p] = ctx

        API = "http://100.84.251.49:8420/api/v1/memory"
        KEY = MEMORY_API_KEY
        DN = device_name

        prompt_parts = []

        # RAG context
        if rag_contexts:
            rag_text = "\n\n".join(
                f"# {p}\n{ctx}" for p, ctx in rag_contexts.items()
            )
            prompt_parts.append((f"## Geçmiş Kararlar — RAG", rag_text, 100))

        prompt_parts.append((f"## Durum — Aktif Bug'lar ({len(bugs)})", bugs_text, 90))
        prompt_parts.append((f"## Okunmamış Notlar", notes_text, 80))
        prompt_parts.append((f"## Hafıza ({len(memories)} kayıt)", mem_text, 60))
        prompt_parts.append((f"## Bağlantı", f"""API: `{API}`
Auth: `X-Memory-Key: {KEY}`
Cihaz: `{DN}` | Platform: `{dev['platform']}` | Oturum: {stats}""", 50))

        context = _truncate_context(prompt_parts, budget=1800)

        prompt = f"""# {dev['name']} ({dev['platform']})

{context}

## Son Oturumlar
{recent_text}

## API Referansı
- Dashboard: `{API}/dashboard`
- Session kaydet: `POST {API}/sessions` device_name={DN}, summary=...
- Bug/fix ekle: `POST {API}/discoveries` project=, type=, title=, details=
- Task ekle: `POST {API}/tasks` project=, task=
- Not ekle/oku: `POST/GET {API}/notes`
- Arama: `GET {API}/search?q=...`

## Kurallar
- Türkçe konuş, onay bekleme, direkt çöz
- Bug/karar bulursan HEMEN kaydet
- Sadece git'ten çıkarılamayan bilgileri kaydet
- Her oturum sonunda session kaydet
"""
        return {"device": device_name, "platform": dev["platform"], "prompt": prompt}
    finally:
        db.close()

@public_router.get("/onboard/{device_name}/raw")
async def get_onboard_prompt_raw(device_name: str):
    result = await get_onboard_prompt(device_name)
    return PlainTextResponse(result["prompt"])

@public_router.get("/onboard/{device_name}/session-context")
async def get_session_context(device_name: str):
    """SessionStart hook için JSON context — budget: ~2000 token.
    Claude Desktop settings.json'daki sessionStart hook'u bu endpoint'i çağırır.
    """
    db = get_db()
    try:
        device = db.execute("SELECT * FROM devices WHERE name=?", (device_name,)).fetchone()
        if not device:
            raise HTTPException(404, f"Device '{device_name}' not found")

        # Son 3 session
        recent = [dict(r) for r in db.execute(
            "SELECT session_num, date, device_name, substr(summary,1,120) as summary "
            "FROM sessions ORDER BY id DESC LIMIT 3"
        ).fetchall()]

        # Aktif bug'lar — sadece title + project
        active_bugs = [dict(r) for r in db.execute(
            "SELECT project, title FROM discoveries WHERE status='active' AND type='bug' ORDER BY created_at DESC LIMIT 10"
        ).fetchall()]

        # Okunmamış notlar
        unread_notes = [dict(r) for r in db.execute(
            "SELECT from_device, title, substr(content,1,200) as content "
            "FROM notes WHERE (to_device=? OR to_device IS NULL) AND read=0 ORDER BY created_at DESC LIMIT 5",
            (device_name,)
        ).fetchall()]

        # Aktif projeler
        projects = [dict(r) for r in db.execute(
            "SELECT project, COUNT(*) as total, "
            "SUM(CASE WHEN type='bug' THEN 1 ELSE 0 END) as bug_count "
            "FROM discoveries WHERE status='active' GROUP BY project ORDER BY total DESC LIMIT 10"
        ).fetchall()]

        # Stale uyarısı
        never_read = db.execute("SELECT COUNT(*) FROM discoveries WHERE read_count=0").fetchone()[0]
        stale_60 = db.execute(
            "SELECT COUNT(*) FROM discoveries WHERE status='active' AND read_count=0 "
            "AND created_at < datetime('now', '-60 days')"
        ).fetchone()[0]

        payload = {
            "device": device_name,
            "platform": device["platform"],
            "session_count": db.execute("SELECT COUNT(*) FROM sessions WHERE device_name=?", (device_name,)).fetchone()[0],
            "recent_sessions": recent,
            "active_bugs": active_bugs,
            "unread_notes": unread_notes,
            "projects": projects,
            "stale": {"never_read": never_read, "stale_60_days": stale_60},
            "token_budget": _TOKEN_BUDGET,
        }
        return payload
    finally:
        db.close()


@public_router.get("/onboard/{device_name}/project-scan")
async def get_project_scan_prompt(device_name: str):
    """Proje tarama prompt'u — proje klasöründe yapıştır, analiz + DB kayıt"""
    db = get_db()
    try:
        device = db.execute("SELECT * FROM devices WHERE name=?", (device_name,)).fetchone()
        if not device:
            raise HTTPException(404, f"Device '{device_name}' not found")

        API = "http://100.84.251.49:8420/api/v1/memory"
        KEY = MEMORY_API_KEY
        DN = device_name

        projects = [r[0] for r in db.execute("SELECT DISTINCT project FROM discoveries ORDER BY project").fetchall()]
        proj_list = ", ".join(projects) if projects else "henüz yok"

        prompt = f"""Bu proje klasörünü analiz et, klipper hafıza DB'sine kaydet.

## Bağlantı
API: {API} | Auth: X-Memory-Key: {KEY} | Cihaz: {DN}
Mevcut projeler: {proj_list}

## Adımlar

**1. Analiz et:**
```bash
pwd && git remote -v 2>/dev/null && git log --oneline -20
```
Proje adı (kısa, küçük harf), stack, test sayısı belirle.

**2. Mevcut kayıt var mı kontrol et:**
```bash
curl -s -H "X-Memory-Key: {KEY}" "{API}/projects/PROJE_ADI"
```
Kayıt varsa sadece eksikleri tamamla.

**3. Cihaz-proje eşle:**
```bash
curl -s -X POST {API}/device-projects -H "Content-Type: application/json" -H "X-Memory-Key: {KEY}" -d '{{"device_name":"{DN}","project":"PROJE","local_path":"YOL"}}'
```

**4. Kaydet** (duplicate korumalı — tekrar göndersen sorun olmaz):

Mimari kararlar (stack seçimi, DB, deploy, tasarım — git'ten çıkarılamayan NEDEN bilgisi):
```bash
curl -s -X POST {API}/discoveries -H "Content-Type: application/json" -H "X-Memory-Key: {KEY}" -d '{{"device_name":"{DN}","project":"P","type":"architecture","title":"BASLIK","details":"DETAY"}}'
```

Planlar (aktif hedefler, roadmap):
```bash
curl -s -X POST {API}/discoveries -H "Content-Type: application/json" -H "X-Memory-Key: {KEY}" -d '{{"device_name":"{DN}","project":"P","type":"plan","title":"BASLIK","details":"DETAY"}}'
```

Bug (bilinen sorunlar): type="bug"
Fix (önemli düzeltmeler — her typo fix değil, sadece önemli olanlar): type="fix"
Workaround (geçici çözümler, neden geçici olduğu): type="workaround"

**5. Önemli task'ler** (son 2 ay, anlamlı iş birimleri — her commit değil):
```bash
curl -s -X POST {API}/tasks -H "Content-Type: application/json" -H "X-Memory-Key: {KEY}" -d '{{"device_name":"{DN}","project":"P","task":"NE_YAPILDI","details":"DETAY"}}'
```

**6. Oturum kaydet + özet ver.**

## Kurallar
- Title max 60 karakter
- Sadece git'ten çıkarılamayan bilgileri kaydet
- "fix: typo" gibi trivial şeyleri KAYDETME
- Onay bekleme, direkt çalış

Başla.
"""
        return PlainTextResponse(prompt)
    finally:
        db.close()
