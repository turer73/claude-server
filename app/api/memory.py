"""
Claude Memory API v2 — Merkezi hafıza sistemi
Duplicate koruması, FTS arama, read tracking, lifecycle yönetimi.
"""
import os
import sqlite3
import json
import re
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, Header
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, field_validator
from typing import Optional, Literal

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
    rationale: Optional[str] = None

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
    rationale: Optional[str] = None

class DiscoveryCreate(BaseModel):
    session_id: Optional[int] = None
    device_name: Optional[str] = "klipper"
    project: str
    type: str
    title: str
    details: Optional[str] = None
    status: Optional[str] = "active"
    rationale: Optional[str] = None

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

class TaskQueueCreate(BaseModel):
    requested_by: str
    target_device: Optional[str] = None
    command: str
    rationale: Optional[str] = None

class TaskQueueClaim(BaseModel):
    claimed_by: str

class TaskQueueResult(BaseModel):
    exit_code: int
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    status: Literal["completed", "failed"] = "completed"

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
                "UPDATE memories SET description=?, content=?, source_device=?, rationale=COALESCE(?, rationale), updated_at=datetime('now') WHERE id=?",
                (data.description, data.content, data.source_device, data.rationale, existing[0])
            )
            db.commit()
            return {"id": existing[0], "status": "updated_existing"}

        cur = db.execute(
            "INSERT INTO memories (type, name, description, content, source_device, rationale) VALUES (?, ?, ?, ?, ?, ?)",
            (data.type, data.name, data.description, data.content, data.source_device, data.rationale))
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


# ============ Tasks Log ============

@router.get("/tasks")
async def list_tasks(project: Optional[str] = None, device: Optional[str] = None, limit: int = 30):
    db = get_db()
    try:
        query = "SELECT id, session_id, device_name, project, task, status, rationale, date(created_at) as date FROM tasks_log WHERE 1=1"
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
            INSERT INTO tasks_log (session_id, device_name, project, task, status, files_changed, details, rationale)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (data.session_id, data.device_name, data.project, data.task, data.status,
              json.dumps(data.files_changed) if data.files_changed else None, data.details, data.rationale))
        db.commit()
        return {"id": cur.lastrowid, "status": "created"}
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
                 "rationale, read_count, date(created_at) as date FROM discoveries WHERE 1=1")
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
    """Duplicate korumalı discovery oluştur — aynı project+type+title aktif kayıt varsa günceller.

    Sadece status='active' kayıtları duplicate olarak kabul edilir. completed/
    obsolete/superseded kayıtlar yeni POST'ları bloklamaz; aynı title ile yeni
    bulgu gelirse regression olarak yeni active row oluşur.
    """
    db = get_db()
    try:
        existing = db.execute(
            "SELECT id FROM discoveries WHERE project=? AND type=? AND title=? AND status='active'",
            (data.project, data.type, data.title)
        ).fetchone()
        if existing:
            # Var olanı güncelle (details veya rationale değiştiyse)
            if data.details or data.rationale:
                db.execute("UPDATE discoveries SET details=COALESCE(?, details), device_name=?, rationale=COALESCE(?, rationale) WHERE id=?",
                           (data.details, data.device_name, data.rationale, existing[0]))
                db.commit()
            return {"id": existing[0], "status": "already_exists"}

        cur = db.execute("""
            INSERT INTO discoveries (session_id, device_name, project, type, title, details, status, rationale)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (data.session_id, data.device_name, data.project, data.type,
              data.title, data.details, data.status or "active", data.rationale))
        db.commit()
        _sync_fts(db, cur.lastrowid, data.title, data.details)
        db.commit()
        return {"id": cur.lastrowid, "status": "created"}
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


# ============ Task Queue ============

@router.get("/queue")
async def list_queue(status: Optional[str] = None, target_device: Optional[str] = None, limit: int = 50):
    db = get_db()
    try:
        q = "SELECT * FROM task_queue WHERE 1=1"
        params = []
        if status:
            q += " AND status=?"
            params.append(status)
        if target_device:
            q += " AND (target_device=? OR target_device IS NULL)"
            params.append(target_device)
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(q, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()


@router.post("/queue")
async def create_queue_task(data: TaskQueueCreate):
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO task_queue (requested_by, target_device, command, rationale) VALUES (?, ?, ?, ?)",
            (data.requested_by, data.target_device, data.command, data.rationale)
        )
        db.commit()
        return {"id": cur.lastrowid, "status": "pending"}
    finally:
        db.close()


@router.put("/queue/{task_id}/claim")
async def claim_queue_task(task_id: int, data: TaskQueueClaim):
    """Atomic claim - only succeeds if task still pending."""
    db = get_db()
    try:
        cur = db.execute(
            "UPDATE task_queue SET status='claimed', claimed_by=?, claimed_at=datetime('now'), started_at=datetime('now') "
            "WHERE id=? AND status='pending'",
            (data.claimed_by, task_id)
        )
        db.commit()
        if cur.rowcount == 0:
            raise HTTPException(409, "Task not in pending state or does not exist")
        row = db.execute("SELECT * FROM task_queue WHERE id=?", (task_id,)).fetchone()
        return dict(row)
    finally:
        db.close()


@router.put("/queue/{task_id}/result")
async def write_queue_result(task_id: int, data: TaskQueueResult):
    """Worker writes back exit code + stdout/stderr."""
    db = get_db()
    try:
        cur = db.execute(
            "UPDATE task_queue SET status=?, exit_code=?, stdout=?, stderr=?, finished_at=datetime('now') "
            "WHERE id=? AND status IN ('claimed', 'running')",
            (data.status, data.exit_code, data.stdout, data.stderr, task_id)
        )
        db.commit()
        if cur.rowcount == 0:
            raise HTTPException(409, "Task not claimed/running or does not exist")
        return {"id": task_id, "status": data.status}
    finally:
        db.close()


# ============ Onboarding Prompts ============

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

        API = "http://127.0.0.1:8420/api/v1/memory"
        KEY = MEMORY_API_KEY
        DN = device_name

        prompt = f"""# Merkezi Hafıza Sistemi — {dev['name']} ({dev['platform']})

Sen benim çoklu cihazda çalışan Claude asistanımsın. Klipper sunucumda merkezi bir hafıza sistemi var.

## Bağlantı
- **API:** `{API}`
- **Auth:** `X-Memory-Key: {KEY}`
- **Cihaz:** `{DN}` | **Platform:** `{dev['platform']}` | **Oturum:** {stats}

## Durum
**Son oturumlar:**
{recent_text}

**Açık bug'lar:**
{bugs_text}

**Notlar:**
{notes_text}

## Hafıza
{mem_text}

## API Kullanımı

**Oturum başı:** `curl -s -H "X-Memory-Key: {KEY}" {API}/dashboard`
**Oturum sonu:** `curl -s -X POST {API}/sessions -H "Content-Type: application/json" -H "X-Memory-Key: {KEY}" -d '{{"device_name":"{DN}","summary":"OZET"}}'`

**Discovery (bug/fix/architecture/plan/workaround/learning/config):**
`curl -s -X POST {API}/discoveries -H "Content-Type: application/json" -H "X-Memory-Key: {KEY}" -d '{{"device_name":"{DN}","project":"PROJE","type":"TIP","title":"BASLIK","details":"DETAY"}}'`
Duplicate korumalı — aynı title varsa günceller.

**Status değiştir:** `curl -s -X PUT {API}/discoveries/ID -H "Content-Type: application/json" -H "X-Memory-Key: {KEY}" -d '{{"status":"completed"}}'`
Status: active, completed, obsolete, superseded

**Task:** `curl -s -X POST {API}/tasks -H "Content-Type: application/json" -H "X-Memory-Key: {KEY}" -d '{{"device_name":"{DN}","project":"PROJE","task":"NE_YAPILDI"}}'`
**Not:** `curl -s -X POST {API}/notes -H "Content-Type: application/json" -H "X-Memory-Key: {KEY}" -d '{{"from_device":"{DN}","title":"BASLIK","content":"ICERIK"}}'`
**Arama (FTS):** `curl -s -H "X-Memory-Key: {KEY}" "{API}/search?q=KELIME"`
**Proje detay:** `curl -s -H "X-Memory-Key: {KEY}" "{API}/projects/PROJE_ADI"`
**Health:** `curl -s -H "X-Memory-Key: {KEY}" "{API}/health"`

## Kurallar
- Türkçe konuş, onay bekleme, direkt çöz
- Bug bulursan HEMEN kaydet, oturum sonunda session kaydet
- Sadece git'ten çıkarılamayan bilgileri kaydet (kararların nedeni, workaround koşulları, projeler arası bağlantılar)
- Co-Authored-By EKLEME (Vercel engelliyor)
- Renderhane push: author turer73 olmalı

Önce dashboard'u kontrol et, sonra nasıl yardımcı olabileceğini sor.
"""
        return {"device": device_name, "platform": dev["platform"], "prompt": prompt}
    finally:
        db.close()

@public_router.get("/onboard/{device_name}/raw")
async def get_onboard_prompt_raw(device_name: str):
    result = await get_onboard_prompt(device_name)
    return PlainTextResponse(result["prompt"])


@public_router.get("/onboard/{device_name}/project-scan")
async def get_project_scan_prompt(device_name: str):
    """Proje tarama prompt'u — proje klasöründe yapıştır, analiz + DB kayıt"""
    db = get_db()
    try:
        device = db.execute("SELECT * FROM devices WHERE name=?", (device_name,)).fetchone()
        if not device:
            raise HTTPException(404, f"Device '{device_name}' not found")

        API = "http://127.0.0.1:8420/api/v1/memory"
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
