"""
Claude Memory API v2 — Merkezi hafıza sistemi
Duplicate koruması, FTS arama, read tracking, lifecycle yönetimi.
"""

import asyncio
import json
import re
import sqlite3
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, field_validator

from app.core.config import read_env_var
from app.core.privacy import redact

DB_PATH = "/opt/linux-ai-server/data/claude_memory.db"

MEMORY_API_KEY = read_env_var("MEMORY_API_KEY")

VALID_DISCOVERY_TYPES = ("bug", "fix", "learning", "config", "workaround", "architecture", "plan")
VALID_STATUSES = ("active", "completed", "obsolete", "superseded")
TRASH_TITLES = re.compile(r"^(test|test bug|test fix|test workaround|deneme|asdf|xxx)$", re.IGNORECASE)


def verify_key(x_memory_key: str = Header(None)):
    # FAIL-CLOSED (güvenlik fix): MEMORY_API_KEY yüklenmemişse erişimi AÇMA.
    # Eski 'if KEY and ...' boş-key'de 401 atmıyordu -> env-yükleme hatasında
    # memory/RAG/research/classifier tamamen korumasız kalıyordu.
    if not MEMORY_API_KEY:
        raise HTTPException(503, "Memory API key not configured (fail-closed)")
    if x_memory_key != MEMORY_API_KEY:
        raise HTTPException(401, "Invalid memory API key")


router = APIRouter(prefix="/api/v1/memory", tags=["memory"], dependencies=[Depends(verify_key)])
# Onboarding endpoints embed MEMORY_API_KEY in their response prompts (so a
# bootstrapped Claude instance has the auth header it needs). They MUST require
# the key on the request side too — otherwise anyone reachable on the LAN /
# Tailscale can curl /onboard/<device> and pull the live API key out of the
# response body.
public_router = APIRouter(prefix="/api/v1/memory", tags=["memory-public"], dependencies=[Depends(verify_key)])


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


_read_by_ready = False


def _ensure_read_by(db):
    """notes.read_by kolonunu idempotent ekle (per-device okuma izleme — #647).
    Eski TEK 'read' kolonu GLOBAL'di: bir device okuyunca herkese okundu sayılıyordu →
    çoğulcu-okuma bozuktu. read_by = '|dev1|dev2|' formatında okuyan-device listesi.
    Backward-compat: legacy read=1 = herkesçe-okunmuş; device'sız mark-read hâlâ read=1 set eder."""
    global _read_by_ready
    if _read_by_ready:
        return
    try:
        cols = [r[1] for r in db.execute("PRAGMA table_info(notes)").fetchall()]
        if "read_by" not in cols:
            db.execute("ALTER TABLE notes ADD COLUMN read_by TEXT DEFAULT ''")
            db.commit()
    except Exception:
        pass
    _read_by_ready = True


def _unread_pred(device):
    """'<device> için okunmamış' SQL parçası + parametreleri. device yoksa legacy global.
    Legacy read=1 (device'sız okunmuş) tüm device'lar için okundu sayılır (geri-uyum)."""
    if device:
        return "read=0 AND (read_by IS NULL OR read_by NOT LIKE ?)", [f"%|{device}|%"]
    return "read=0", []


# ============ Event / Webhook / Telegram Helpers ============

_WEBHOOK_TIMEOUT = 5
_TOKEN_BUDGET = 2000
_TELEGRAM_BOT_TOKEN = read_env_var("TELEGRAM_BOT_TOKEN")
_TELEGRAM_CHAT_ID = read_env_var("TELEGRAM_CHAT_ID")
_TELEGRAM_EVENTS = read_env_var("MEMORY_TELEGRAM_EVENTS")  # bos: kapali, "bug,fix,task,note" gibi


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
    if not _TELEGRAM_BOT_TOKEN or not _TELEGRAM_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"https://api.telegram.org/bot{_TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": _TELEGRAM_CHAT_ID, "text": message, "parse_mode": parse_mode, "disable_web_page_preview": True},
            )
    except Exception:
        pass


async def _fire_event(event: str, payload: dict):
    try:
        db = get_db()
        _ensure_webhooks_table(db)
        hooks = db.execute("SELECT url, secret FROM webhooks WHERE event=? AND active=1", (event,)).fetchall()
        db.close()
        if hooks:
            async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT) as client:
                tasks = []
                for url, secret in hooks:
                    h = {"Content-Type": "application/json"}
                    if secret:
                        h["X-Webhook-Secret"] = secret
                    tasks.append(client.post(url, json=payload, headers=h))
                await asyncio.gather(*tasks, return_exceptions=True)
        allowed = _TELEGRAM_EVENTS.split(",") if _TELEGRAM_EVENTS else []
        if not allowed:
            return
        event_name = event.removesuffix("_created")
        if event_name not in allowed:
            return
        if event == "bug_created":
            await _send_telegram(
                f"<b>\U0001f41b Yeni Bug!</b>\n"
                f"Proje: <code>{payload.get('project', '?')}</code>\n"
                f"Ba\u015fl\u0131k: {payload.get('title', '?')[:200]}"
            )
        elif event == "fix_created":
            await _send_telegram(
                f"<b>\U0001f527 Yeni Fix</b>\n"
                f"Proje: <code>{payload.get('project', '?')}</code>\n"
                f"Ba\u015fl\u0131k: {payload.get('title', '?')[:200]}"
            )
        elif event == "task_created":
            await _send_telegram(
                f"<b>\U0001f4cb Yeni Task</b>\nProje: <code>{payload.get('project', '?')}</code>\nTask: {payload.get('task', '?')[:200]}"
            )
        elif event == "note_created":
            await _send_telegram(
                f"<b>\U0001f4dd Yeni Not</b>\n"
                f"G\u00f6nderen: <code>{payload.get('from_device', '?')}</code>\n"
                f"{payload.get('title', '?')[:200]}"
            )
    except Exception:
        pass


# ============ Context Helpers ============


def _estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1


def _truncate_context(items: list[tuple[str, str, int]], budget: int = _TOKEN_BUDGET) -> str:
    parts = []
    used = 0
    for label, content, _score in sorted(items, key=lambda x: -x[2]):
        t = _estimate_tokens(content)
        if used + t > budget:
            chars = (budget - used) * 4
            parts.append(f"## {label}\n{content[:chars]}...")
            break
        parts.append(f"## {label}\n{content}")
        used += t
    return "\n\n".join(parts)


def _track_read(db, table: str, row_id: int):
    """Read tracking — her okumada sayaç artır"""
    db.execute(f"UPDATE {table} SET read_count=read_count+1, last_read_at=datetime('now') WHERE id=?", (row_id,))
    db.commit()


def _sync_fts(db, disc_id: int, title: str, details: str = ""):
    """FTS index güncelle"""
    try:
        db.execute("INSERT INTO discoveries_fts(rowid, title, details) VALUES (?, ?, ?)", (disc_id, title, details or ""))
    except Exception:
        pass


# ============ Models ============


class DeviceRegister(BaseModel):
    name: str
    platform: str
    hostname: str | None = None
    ip: str | None = None
    tailscale_ip: str | None = None
    os_version: str | None = None
    claude_version: str | None = None
    notes: str | None = None


class SessionCreate(BaseModel):
    device_name: str
    session_num: int | None = None
    summary: str
    tasks_completed: list | None = None
    files_changed: list | None = None
    bugs_found: list | None = None
    notes: str | None = None


class MemoryCreate(BaseModel):
    type: Literal["user", "feedback", "project", "reference"]
    name: str
    description: str
    content: str
    source_device: str | None = "klipper"
    rationale: str | None = None


class MemoryUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    content: str | None = None
    active: int | None = None


class TaskLogCreate(BaseModel):
    session_id: int | None = None
    device_name: str | None = "klipper"
    project: str
    task: str
    status: str | None = "completed"
    files_changed: list | None = None
    details: str | None = None
    rationale: str | None = None


class TaskLogUpdate(BaseModel):
    status: Literal["completed", "obsolete", "failed", "pending", "in_progress"] | None = None
    rationale: str | None = None


class DiscoveryCreate(BaseModel):
    session_id: int | None = None
    device_name: str | None = "klipper"
    project: str
    type: str
    title: str
    details: str | None = None
    status: str | None = "active"
    rationale: str | None = None

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
    title: str | None = None
    details: str | None = None
    status: str | None = None

    @field_validator("status")
    @classmethod
    def valid_status(cls, v):
        if v and v not in VALID_STATUSES:
            raise ValueError(f"Geçersiz status: {v}. Geçerli: {', '.join(VALID_STATUSES)}")
        return v


class NoteCreate(BaseModel):
    from_device: str
    to_device: str | None = None
    title: str
    content: str


class DeviceProjectCreate(BaseModel):
    device_name: str
    project: str
    local_path: str | None = None


class SpawnFailureRetryResponse(BaseModel):
    id: int
    note_id: int
    status: str
    message: str


# NOTE: SecretSet model moved to app/api/admin.py (single source).


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

        devices = [
            dict(r)
            for r in db.execute("SELECT name, platform, hostname, tailscale_ip, last_seen FROM devices ORDER BY last_seen DESC").fetchall()
        ]

        recent_sessions = [
            dict(r)
            for r in db.execute(
                "SELECT session_num, date, device_name, platform, substr(summary,1,100) as summary FROM sessions ORDER BY id DESC LIMIT 5"
            ).fetchall()
        ]

        open_bugs = [
            dict(r)
            for r in db.execute(
                "SELECT id, project, title, device_name, created_at FROM discoveries "
                "WHERE type='bug' AND status='active' ORDER BY created_at DESC"
            ).fetchall()
        ]

        # Stale data — 60+ gün okunamayan active kayıtlar
        stale = [
            dict(r)
            for r in db.execute(
                "SELECT id, project, type, title, date(created_at) as created, read_count "
                "FROM discoveries WHERE status='active' AND read_count=0 "
                "AND created_at < datetime('now', '-60 days') ORDER BY created_at LIMIT 10"
            ).fetchall()
        ]

        # Hiç okunmamış kayıt sayısı
        never_read = db.execute("SELECT COUNT(*) FROM discoveries WHERE read_count=0").fetchone()[0]

        # Proje bazlı özet
        projects = [
            dict(r)
            for r in db.execute(
                "SELECT project, COUNT(*) as total, "
                "SUM(CASE WHEN type='bug' AND status='active' THEN 1 ELSE 0 END) as open_bugs, "
                "SUM(CASE WHEN type='architecture' THEN 1 ELSE 0 END) as arch, "
                "SUM(CASE WHEN type='plan' AND status='active' THEN 1 ELSE 0 END) as active_plans "
                "FROM discoveries GROUP BY project ORDER BY total DESC"
            ).fetchall()
        ]

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


# ============ Memories ============


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


@router.get("/surface")
async def memory_surface(
    type: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Sentez-sonrası YÜZEY: aktif + canonical (merged olmayan) memory'ler (LIVESYS-MEMSYN).
    P0-c (surer): SAYFALANMIŞ — default 100 (max 500). 629-korpus limit'siz ~48K-token bomba
    (LLM-context'i doldurur). Yanıt {total, count, limit, offset, items}; items kapalı-uçlu."""
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


# ============ Sessions ============


@router.get("/sessions")
async def list_sessions(device: str | None = None, platform: str | None = None, limit: int = 20):
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

        cur = db.execute(
            """
            INSERT INTO sessions (session_num, date, summary, tasks_completed, files_changed, bugs_found, notes, device_id, platform, device_name)
            VALUES (?, date('now'), ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                data.session_num,
                data.summary,
                json.dumps(data.tasks_completed) if data.tasks_completed else None,
                json.dumps(data.files_changed) if data.files_changed else None,
                json.dumps(data.bugs_found) if data.bugs_found else None,
                data.notes,
                device_id,
                platform,
                data.device_name,
            ),
        )
        db.commit()

        if device_id:
            db.execute("UPDATE devices SET last_seen=datetime('now') WHERE id=?", (device_id,))
            db.commit()

        asyncio.create_task(
            _fire_event(
                "session_created",
                {
                    "session_id": cur.lastrowid,
                    "device": data.device_name,
                },
            )
        )

        return {"id": cur.lastrowid, "session_num": data.session_num, "status": "created"}
    finally:
        db.close()


# ============ Tasks Log ============


@router.get("/tasks")
async def list_tasks(project: str | None = None, device: str | None = None, limit: int = 30):
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
        existing = db.execute("SELECT id FROM tasks_log WHERE project=? AND task=?", (data.project, data.task)).fetchone()
        if existing:
            return {"id": existing[0], "status": "already_exists"}

        cur = db.execute(
            """
            INSERT INTO tasks_log (session_id, device_name, project, task, status, files_changed, details, rationale)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                data.session_id,
                data.device_name,
                data.project,
                data.task,
                data.status,
                json.dumps(data.files_changed) if data.files_changed else None,
                data.details,
                data.rationale,
            ),
        )
        db.commit()

        asyncio.create_task(
            _fire_event(
                "task_created",
                {
                    "id": cur.lastrowid,
                    "project": data.project,
                    "task": data.task,
                    "status": data.status,
                    "device": data.device_name,
                },
            )
        )

        return {"id": cur.lastrowid, "status": "created"}
    finally:
        db.close()


@router.patch("/tasks/{task_id}")
async def update_task_log(task_id: int, data: TaskLogUpdate):
    if data.status is None and data.rationale is None:
        raise HTTPException(400, "En az status veya rationale gönderin")
    db = get_db()
    try:
        row = db.execute("SELECT id, status FROM tasks_log WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Task {task_id} bulunamadı")
        sets, params = [], []
        if data.status is not None:
            sets.append("status=?")
            params.append(data.status)
        if data.rationale is not None:
            sets.append("rationale=?")
            params.append(data.rationale)
        params.append(task_id)
        db.execute(f"UPDATE tasks_log SET {', '.join(sets)} WHERE id=?", params)
        db.commit()
        new_status = data.status if data.status is not None else row["status"]
        return {"id": task_id, "new_status": new_status, "status": "updated"}
    finally:
        db.close()


# ============ Discoveries ============


@router.get("/discoveries")
async def list_discoveries(project: str | None = None, type: str | None = None, status: str | None = None, limit: int = 30):
    db = get_db()
    try:
        query = (
            "SELECT id, session_id, device_name, project, type, title, status, "
            "rationale, read_count, date(created_at) as date FROM discoveries WHERE 1=1"
        )
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

    Iki ekstra koruma:
    - Privacy: details icindeki secret/token redact edilir (app.core.privacy).
    - 5dk dedup window: ayni project+title+details son 5dk icinde varsa skip.
    """
    details_clean, redacted_labels = redact(data.details)

    db = get_db()
    try:
        # 5-dakika exact-match dedup window
        recent_dup = db.execute(
            "SELECT id FROM discoveries WHERE project=? AND type=? AND title=? "
            "AND COALESCE(details,'')=? "
            "AND created_at > datetime('now','-5 minutes')",
            (data.project, data.type, data.title, details_clean or ""),
        ).fetchone()
        if recent_dup:
            return {
                "id": recent_dup[0],
                "status": "duplicate_skipped_5min",
                "secrets_redacted": redacted_labels,
            }

        existing = db.execute(
            "SELECT id FROM discoveries WHERE project=? AND type=? AND title=? AND status='active'", (data.project, data.type, data.title)
        ).fetchone()
        if existing:
            # Var olanı güncelle (details veya rationale değiştiyse)
            if details_clean or data.rationale:
                db.execute(
                    "UPDATE discoveries SET details=COALESCE(?, details), device_name=?, rationale=COALESCE(?, rationale) WHERE id=?",
                    (details_clean, data.device_name, data.rationale, existing[0]),
                )
                db.commit()
            return {"id": existing[0], "status": "already_exists", "secrets_redacted": redacted_labels}

        cur = db.execute(
            """
            INSERT INTO discoveries (session_id, device_name, project, type, title, details, status, rationale)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                data.session_id,
                data.device_name,
                data.project,
                data.type,
                data.title,
                details_clean,
                data.status or "active",
                data.rationale,
            ),
        )
        db.commit()
        _sync_fts(db, cur.lastrowid, data.title, details_clean)
        db.commit()
        new_id = cur.lastrowid

        event_type = f"{data.type}_created" if data.type in ("bug", "fix") else "discovery_created"
        asyncio.create_task(
            _fire_event(
                event_type,
                {
                    "id": new_id,
                    "project": data.project,
                    "type": data.type,
                    "title": data.title,
                    "device": data.device_name,
                },
            )
        )

        return {"id": new_id, "status": "created", "secrets_redacted": redacted_labels}
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
async def list_discoveries_by_type(dtype: str, project: str | None = None, status: str | None = "active"):
    db = get_db()
    try:
        query = (
            "SELECT id, project, type, title, details, status, read_count, "
            "device_name, date(created_at) as date FROM discoveries WHERE type=?"
        )
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
                projects[p] = {
                    "name": p,
                    "open_bugs": 0,
                    "fixes": 0,
                    "architecture": 0,
                    "active_plans": 0,
                    "completed_plans": 0,
                    "workarounds": 0,
                    "tasks": 0,
                }
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
                projects[row[0]] = {
                    "name": row[0],
                    "open_bugs": 0,
                    "fixes": 0,
                    "architecture": 0,
                    "active_plans": 0,
                    "completed_plans": 0,
                    "workarounds": 0,
                    "tasks": row[1],
                }

        # Health skoru: mimari var, plan var, bug az = sağlıklı
        for p in projects.values():
            score = 0
            if p["architecture"] > 0:
                score += 30
            if p["active_plans"] > 0 or p["completed_plans"] > 0:
                score += 20
            if p["tasks"] > 0:
                score += 20
            if p["fixes"] > 0:
                score += 15
            if p["open_bugs"] == 0:
                score += 15
            elif p["open_bugs"] <= 2:
                score += 5
            p["health"] = min(score, 100)

        return sorted(projects.values(), key=lambda x: x["health"], reverse=True)
    finally:
        db.close()


@router.get("/projects/{project_name}")
async def get_project_detail(project_name: str):
    """Proje detayı — discoveries, tasks, sessions, health"""
    db = get_db()
    try:
        discoveries = [
            dict(r)
            for r in db.execute(
                "SELECT id, type, title, details, status, read_count, device_name, date(created_at) as date "
                "FROM discoveries WHERE project=? ORDER BY type, created_at DESC",
                (project_name,),
            ).fetchall()
        ]

        tasks = [
            dict(r)
            for r in db.execute(
                "SELECT id, task, status, device_name, details, date(created_at) as date "
                "FROM tasks_log WHERE project=? ORDER BY created_at DESC LIMIT 30",
                (project_name,),
            ).fetchall()
        ]

        sessions = [
            dict(r)
            for r in db.execute(
                "SELECT id, session_num, date, device_name, platform, substr(summary,1,120) as summary "
                "FROM sessions WHERE summary LIKE ? ORDER BY id DESC LIMIT 10",
                (f"%{project_name}%",),
            ).fetchall()
        ]

        devices = [
            dict(r)
            for r in db.execute(
                "SELECT device_name, local_path, datetime(last_activity) as last_activity FROM device_projects WHERE project=?",
                (project_name,),
            ).fetchall()
        ]

        type_counts = {}
        for d in discoveries:
            key = f"{d['type']}_{d['status']}" if d["status"] != "active" else d["type"]
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


# ============ Device Projects ============


@router.get("/device-projects")
async def list_device_projects(device: str | None = None):
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
        db.execute(
            """
            INSERT INTO device_projects (device_name, project, local_path)
            VALUES (?, ?, ?)
            ON CONFLICT(device_name, project) DO UPDATE SET
                local_path=excluded.local_path, last_activity=datetime('now')
        """,
            (data.device_name, data.project, data.local_path),
        )
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()


# ============ Webhooks ============


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
    db = get_db()
    try:
        _ensure_webhooks_table(db)
        existing = db.execute("SELECT id FROM webhooks WHERE event=? AND url=?", (event, url)).fetchone()
        if existing:
            return {"id": existing[0], "status": "already_exists"}
        cur = db.execute("INSERT INTO webhooks (event, url, secret) VALUES (?, ?, ?)", (event, url, secret))
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
    if not _TELEGRAM_BOT_TOKEN:
        return {"configured": False, "message": "TELEGRAM_BOT_TOKEN env eksik"}
    if not _TELEGRAM_CHAT_ID:
        return {"configured": False, "message": "TELEGRAM_CHAT_ID env eksik"}
    await _send_telegram("✅ <b>Klipper Haf\u0131za Sistemi</b>\nTelegram bildirimleri aktif!")
    return {"configured": True, "message": "Test mesaj\u0131 g\u00f6nderildi"}


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
                (q,),
            ).fetchall()
            fts_ids = [r[0] for r in fts_rows]
            if fts_ids:
                placeholders = ",".join("?" * len(fts_ids))
                results["discoveries"] = [
                    dict(r)
                    for r in db.execute(
                        f"SELECT id, project, type, title, status, device_name, date(created_at) as date "
                        f"FROM discoveries WHERE id IN ({placeholders})",
                        fts_ids,
                    ).fetchall()
                ]
            else:
                results["discoveries"] = []
        except Exception:
            # FTS fallback → LIKE
            pattern = f"%{q}%"
            results["discoveries"] = [
                dict(r)
                for r in db.execute(
                    "SELECT id, project, type, title, status, device_name FROM discoveries WHERE title LIKE ? OR details LIKE ? LIMIT 15",
                    (pattern, pattern),
                ).fetchall()
            ]

        # Memories — LIKE
        pattern = f"%{q}%"
        results["memories"] = [
            dict(r)
            for r in db.execute(
                "SELECT id, type, name, description FROM memories WHERE active=1 AND (content LIKE ? OR name LIKE ?) LIMIT 10",
                (pattern, pattern),
            ).fetchall()
        ]

        # Sessions — LIKE
        results["sessions"] = [
            dict(r)
            for r in db.execute(
                "SELECT id, session_num, date, device_name, substr(summary,1,100) as summary FROM sessions "
                "WHERE summary LIKE ? OR tasks_completed LIKE ? LIMIT 10",
                (pattern, pattern),
            ).fetchall()
        ]

        # Tasks — LIKE
        results["tasks"] = [
            dict(r)
            for r in db.execute(
                "SELECT id, project, task, device_name FROM tasks_log WHERE task LIKE ? OR details LIKE ? LIMIT 10", (pattern, pattern)
            ).fetchall()
        ]

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
        active_total = db.execute("SELECT COUNT(*) FROM discoveries WHERE status='active'").fetchone()[0]
        never_read = db.execute("SELECT COUNT(*) FROM discoveries WHERE read_count=0").fetchone()[0]
        # Sağlık metriği YALNIZ aktif kayıtlara dayanır. Obsolete/closed kayıtların
        # okunmamış olması beklenir ve aksiyonluk değildir — ham never_read/total
        # oranı bu yüzden yanıltıcı (~%86) çıkıyordu. Gerçek temizlik sinyali = aktif okunmamış.
        active_never_read = db.execute("SELECT COUNT(*) FROM discoveries WHERE status='active' AND read_count=0").fetchone()[0]
        stale_60 = db.execute(
            "SELECT COUNT(*) FROM discoveries WHERE status='active' AND created_at < datetime('now', '-60 days')"
        ).fetchone()[0]
        most_read = [
            dict(r)
            for r in db.execute("SELECT id, project, type, title, read_count FROM discoveries ORDER BY read_count DESC LIMIT 5").fetchall()
        ]
        never_read_pct = round(active_never_read / active_total * 100, 1) if active_total > 0 else 0

        return {
            "total_discoveries": total,
            "active_discoveries": active_total,
            "never_read": never_read,
            "active_never_read": active_never_read,
            "never_read_pct": never_read_pct,
            "stale_60_days": stale_60,
            "most_read": most_read,
            "recommendation": "Sistem sağlıklı" if never_read_pct < 50 else "Çok fazla okunmayan aktif kayıt — temizlik gerekiyor",
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
            (f"-{days}",),
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
        stale_count = 0
        if not dry_run:
            cur = db.execute(
                "UPDATE discoveries SET status='obsolete' WHERE status='active' AND read_count=0 "
                "AND type NOT IN ('bug') AND created_at < datetime('now', ? || ' days')",
                (f"-{days}",),
            )
            stale_count = cur.rowcount
        else:
            stale_count = db.execute(
                "SELECT COUNT(*) FROM discoveries WHERE status='active' AND read_count=0 "
                "AND type NOT IN ('bug') AND created_at < datetime('now', ? || ' days')",
                (f"-{days}",),
            ).fetchone()[0]

        if not dry_run:
            db.execute("DELETE FROM discoveries_fts WHERE rowid NOT IN (SELECT id FROM discoveries)")
            db.commit()

        total = db.execute("SELECT COUNT(*) FROM discoveries").fetchone()[0]
        active_total = db.execute("SELECT COUNT(*) FROM discoveries WHERE status='active'").fetchone()[0]
        never_read = db.execute("SELECT COUNT(*) FROM discoveries WHERE read_count=0").fetchone()[0]
        active_never_read = db.execute("SELECT COUNT(*) FROM discoveries WHERE status='active' AND read_count=0").fetchone()[0]
        active_bugs = db.execute("SELECT COUNT(*) FROM discoveries WHERE type='bug' AND status='active'").fetchone()[0]

        report = {
            "action": "dry_run" if dry_run else "cleanup",
            "stale_archived": stale_count,
            "total_discoveries": total,
            "active_discoveries": active_total,
            "never_read": never_read,
            "active_never_read": active_never_read,
            "active_bugs": active_bugs,
            # Aktif-kapsamlı oran (yanıltıcı ham %86 yerine gerçek temizlik sinyali)
            "never_read_pct": round(active_never_read / max(active_total, 1) * 100, 1),
        }

        if not dry_run:
            await _send_telegram(
                f"<b>\U0001f9f9 Klipper Bak\u0131m Raporu</b>\n"
                f"Ar\u015fivlenen: {stale_count} kay\u0131t\n"
                f"Kalan: {total} discovery, {active_bugs} aktif bug\n"
                f"Okunmam\u0131\u015f: {never_read} (%{report['never_read_pct']})"
            )

        return report
    finally:
        db.close()


@router.get("/maintenance/detect-conflicts")
async def detect_conflicts():
    db = get_db()
    try:
        stale_bugs = [
            dict(r)
            for r in db.execute(
                "SELECT id, project, title, status FROM discoveries "
                "WHERE type='bug' AND status='active' AND title LIKE '%COZULDU%' ORDER BY project"
            ).fetchall()
        ]

        dups = [
            dict(r)
            for r in db.execute(
                "SELECT project, type, title, COUNT(*) as cnt, GROUP_CONCAT(id) as ids "
                "FROM discoveries GROUP BY project, type, title HAVING cnt > 1 ORDER BY cnt DESC LIMIT 20"
            ).fetchall()
        ]

        return {
            "stale_bugs_cozuldu": stale_bugs,
            "duplicate_discoveries": dups,
            "total_stale": len(stale_bugs),
            "total_dups": len(dups),
        }
    finally:
        db.close()


# NOTE: Secrets endpoints moved to app/api/admin.py — they use JWT auth
# (require_auth) for dashboard compatibility, separate from the X-Memory-Key
# auth this router uses.
#
# NOTE: Task Queue endpoints (GET/POST /queue, PUT /queue/{id}/claim, /result)
# removed 2026-05-25 along with task_queue table — 1 ay kullanilmadi, smoke
# test'ten oteye gecmedi. Aktif iş günlüğü tasks_log (/tasks endpoint'leri).


# ============ DLQ: Spawn Failures (P0.2) ============


@router.get("/spawn-failures")
async def list_spawn_failures(
    status: str | None = Query(None, regex="^(pending_retry|poison|archived|orphaned)$"),
    limit: int = Query(50, ge=1, le=200),
):
    """Autonomous Claude spawn fail DLQ listesi. Filter: status."""
    db = get_db()
    try:
        q = "SELECT * FROM spawn_failures WHERE 1=1"
        params: list = []
        if status:
            q += " AND status=?"
            params.append(status)
        q += " ORDER BY first_failed_at DESC LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in db.execute(q, params).fetchall()]
        return {"count": len(rows), "rows": rows}
    finally:
        db.close()


@router.post("/spawn-failures/{failure_id}/retry")
async def retry_spawn_failure(failure_id: int):
    """
    Manuel retry: DLQ row'unu pending_retry'a geri al (attempt_num=0 reset, fresh start).
    Bir sonraki cron tick'inde (~15dk) hemen cekilir.
    """
    db = get_db()
    try:
        row = db.execute(
            "SELECT id, note_id, status FROM spawn_failures WHERE id=?",
            (failure_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "spawn_failure not found")
        if row["status"] == "archived":
            return SpawnFailureRetryResponse(
                id=row["id"],
                note_id=row["note_id"],
                status="archived",
                message="Already archived (success). No-op.",
            ).model_dump()
        db.execute(
            "UPDATE spawn_failures SET status='pending_retry', last_retry_at=NULL, attempt_num=0 WHERE id=?",
            (failure_id,),
        )
        db.commit()
        return SpawnFailureRetryResponse(
            id=row["id"],
            note_id=row["note_id"],
            status="pending_retry",
            message="Reset for retry. Next cron tick (~15min) will pick up.",
        ).model_dump()
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
            "SELECT session_num, date, device_name, substr(summary,1,80) as summary FROM sessions ORDER BY id DESC LIMIT 5"
        ).fetchall()
        recent_text = "\n".join(f"  - #{r[0]} ({r[2]}, {r[1]}): {r[3]}" for r in recent)

        bugs = db.execute("SELECT project, title, device_name FROM discoveries WHERE type='bug' AND status='active'").fetchall()
        bugs_text = "\n".join(f"  - [{r[0]}] {r[1]} (bulan: {r[2]})" for r in bugs) if bugs else "  Yok"

        _ensure_read_by(db)
        _pred, _pp = _unread_pred(device_name)  # PER-DEVICE okunmamış (#647)
        notes = db.execute(
            f"SELECT from_device, title, content FROM notes WHERE (to_device=? OR to_device IS NULL) AND {_pred}",
            (device_name, *_pp),
        ).fetchall()
        notes_text = "\n".join(f"  - {r[0]}: {r[1]} — {r[2]}" for r in notes) if notes else "  Yok"

        memories = db.execute("SELECT type, name, content FROM memories WHERE active=1 ORDER BY type").fetchall()
        mem_text = "\n".join(f"  [{r[0]}] {r[1]}: {r[2][:120]}" for r in memories)

        stats = db.execute("SELECT COUNT(*) FROM sessions WHERE device_name=?", (device_name,)).fetchone()[0]

        API = "http://127.0.0.1:8420/api/v1/memory"
        KEY = MEMORY_API_KEY
        DN = device_name

        prompt = f"""# Merkezi Hafıza Sistemi — {dev["name"]} ({dev["platform"]})

Sen benim çoklu cihazda çalışan Claude asistanımsın. Klipper sunucumda merkezi bir hafıza sistemi var.

## Bağlantı
- **API:** `{API}`
- **Auth:** `X-Memory-Key: {KEY}`
- **Cihaz:** `{DN}` | **Platform:** `{dev["platform"]}` | **Oturum:** {stats}

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

        # RAG context (aktif projeler için)
        _RAG_BASE = "http://localhost:8420/api/v1/rag"
        try:
            active_projects = list(
                {
                    r[0]
                    for r in db.execute(
                        "SELECT project FROM discoveries WHERE status='active' ORDER BY created_at DESC LIMIT 10"
                    ).fetchall()
                }
            )
            rag_sections = []
            for proj in active_projects[:3]:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.post(
                        f"{_RAG_BASE}/search",
                        json={"q": f"{proj} nedir ne durumda", "top_k": 2, "project": proj},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        results = data.get("results", []) if "results" in data else data
                        if results and isinstance(results, list):
                            texts = [r.get("text", str(r))[:200] for r in results[:2] if r.get("text")]
                            if texts:
                                rag_sections.append(f"# {proj}\n" + "\n".join(f"  - {t}" for t in texts))
            if rag_sections:
                prompt += "\n\n## Geçmiş Kararlar (RAG)\n" + "\n\n".join(rag_sections)
        except Exception:
            pass

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


@public_router.get("/onboard/{device_name}/session-context")
async def get_session_context(device_name: str):
    """SessionStart hook için JSON context — budget: ~2000 token."""
    db = get_db()
    try:
        device = db.execute("SELECT * FROM devices WHERE name=?", (device_name,)).fetchone()
        if not device:
            raise HTTPException(404, f"Device '{device_name}' not found")

        recent = [
            dict(r)
            for r in db.execute(
                "SELECT session_num, date, device_name, substr(summary,1,120) as summary FROM sessions ORDER BY id DESC LIMIT 3"
            ).fetchall()
        ]

        active_bugs = [
            dict(r)
            for r in db.execute(
                "SELECT project, title FROM discoveries WHERE status='active' AND type='bug' ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
        ]

        _ensure_read_by(db)
        _pred, _pp = _unread_pred(device_name)
        unread_notes = [
            dict(r)
            for r in db.execute(
                f"SELECT from_device, title, substr(content,1,200) as content "
                f"FROM notes WHERE (to_device=? OR to_device IS NULL) AND {_pred} ORDER BY created_at DESC LIMIT 5",
                (device_name, *_pp),
            ).fetchall()
        ]

        projects = [
            dict(r)
            for r in db.execute(
                "SELECT project, COUNT(*) as total, "
                "SUM(CASE WHEN type='bug' THEN 1 ELSE 0 END) as bug_count "
                "FROM discoveries WHERE status='active' GROUP BY project ORDER BY total DESC LIMIT 10"
            ).fetchall()
        ]

        never_read = db.execute("SELECT COUNT(*) FROM discoveries WHERE read_count=0").fetchone()[0]
        stale_60 = db.execute(
            "SELECT COUNT(*) FROM discoveries WHERE status='active' AND read_count=0 AND created_at < datetime('now', '-60 days')"
        ).fetchone()[0]

        return {
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
    finally:
        db.close()
