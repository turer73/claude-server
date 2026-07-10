"""
Claude Memory API v2 — Merkezi hafıza sistemi
Duplicate koruması, FTS arama, read tracking, lifecycle yönetimi.
"""

import asyncio
import hashlib
import re
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, field_validator

from app.core.config import read_env_var
from app.db.data_layer import MEMORY_DB, get_conn

DB_PATH = MEMORY_DB

MEMORY_API_KEY = read_env_var("MEMORY_API_KEY")
# GAP-1 item-D (#1222 A-2): DISTINCT otonom-key. Otonom-spawn bu key ile auth olur;
# create_note from_device'i 'klipper-autonomous'a ZORLA-override eder (unforgeable —
# spawn body'de ne derse desin). Set edilmemisse ozellik dormant (geriye-uyumlu).
MEMORY_API_KEY_AUTONOMOUS = read_env_var("MEMORY_API_KEY_AUTONOMOUS")
AUTONOMOUS_FROM_DEVICE = "klipper-autonomous"

VALID_DISCOVERY_TYPES = ("bug", "fix", "learning", "config", "workaround", "architecture", "plan")
VALID_STATUSES = ("active", "completed", "obsolete", "superseded")
TRASH_TITLES = re.compile(r"^(test|test bug|test fix|test workaround|deneme|asdf|xxx)$", re.IGNORECASE)


def _is_autonomous_key(x_memory_key: str | None) -> bool:
    """Istek DISTINCT otonom-key ile mi auth oldu. Bos-key asla otonom sayilmaz.
    KEY-COLLISION GUARD (Codex Tier-1 #2, fail-CLOSED): otonom-key == master-key (config-hatasi)
    ise otonom-mod DEVRE-DISI (yoksa HER normal-POST force-tag'lenir, attribution/dedup bozulur)."""
    return bool(MEMORY_API_KEY_AUTONOMOUS) and MEMORY_API_KEY_AUTONOMOUS != MEMORY_API_KEY and x_memory_key == MEMORY_API_KEY_AUTONOMOUS


# ── P0 kimlik (konu-1 karari, Turgut onayi): per-device API-key ──
# Otonom-key deseninin (GAP-1 A-2 unforgeable) TUM cihazlara genellenmesi: her cihazin
# AYRI key'i, from_device sunucu-tarafinda KEY'DEN turetilir (client-iddiasi degil).
# Key'ler duz saklanmaz — sha256 hash (credential-plan-docs dersi). Master-key legacy
# calismaya devam eder (kilitleme yok, kademeli gecis) ama yazdiklari verified=0 kalir.

_device_keys_ready = False


def _ensure_device_keys(db: Any) -> None:
    """device_keys tablosunu idempotent kur (_ensure_read_by deseni)."""
    global _device_keys_ready
    if _device_keys_ready:
        return
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS device_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device TEXT NOT NULL UNIQUE,
            key_hash TEXT NOT NULL UNIQUE,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            last_used_at TEXT
        )""")
        db.commit()
    except Exception:
        pass
    _device_keys_ready = True


def _resolve_device_key(x_memory_key: str | None) -> str:
    """Key aktif bir device-key ise cihaz adini dondur, degilse ''.
    Master/otonom-key burada COZULMEZ (oncelik verify_key/dispatch_origin'de) — master
    string'i yanlislikla device-key yapilirsa master-yolu kazanir (fail-safe, collision-guard
    deseniyle tutarli). Her cagride SELECT: lokal SQLite, cache-invalidation karmasasina degmez."""
    if not x_memory_key or x_memory_key == MEMORY_API_KEY or _is_autonomous_key(x_memory_key):
        return ""
    key_hash = hashlib.sha256(x_memory_key.encode()).hexdigest()
    try:
        db = get_db()
        try:
            _ensure_device_keys(db)
            row = db.execute("SELECT device FROM device_keys WHERE key_hash=? AND active=1", (key_hash,)).fetchone()
            if row:
                db.execute("UPDATE device_keys SET last_used_at=datetime('now') WHERE key_hash=?", (key_hash,))
                db.commit()
                return str(row[0])
        finally:
            db.close()
    except Exception:
        return ""
    return ""


def verify_key(x_memory_key: str = Header(None)):
    # FAIL-CLOSED (güvenlik fix): MEMORY_API_KEY yüklenmemişse erişimi AÇMA.
    # Eski 'if KEY and ...' boş-key'de 401 atmıyordu -> env-yükleme hatasında
    # memory/RAG/research/classifier tamamen korumasız kalıyordu.
    if not MEMORY_API_KEY:
        raise HTTPException(503, "Memory API key not configured (fail-closed)")
    # Master VEYA distinct-otonom-key VEYA aktif per-device-key kabul.
    if x_memory_key == MEMORY_API_KEY or _is_autonomous_key(x_memory_key) or _resolve_device_key(x_memory_key):
        return
    raise HTTPException(401, "Invalid memory API key")


def verify_master_key(x_memory_key: str = Header(None)) -> None:
    """MASTER-key ZORUNLU — otonom-key REDDEDILIR (Codex Tier-1 #1). Onboarding/key-SIZAN
    route'lar icin: onboarding yaniti MASTER-key gomuyor; otonom-key bu endpoint'lere erisip
    master'i ogrenip force-tag'i BYPASS etmesin (unforgeable-garanti korunur)."""
    if not MEMORY_API_KEY:
        raise HTTPException(503, "Memory API key not configured (fail-closed)")
    if x_memory_key != MEMORY_API_KEY:
        raise HTTPException(401, "Invalid memory API key (master required)")


def dispatch_origin(x_memory_key: str = Header(None)) -> str:
    """create_note icin FastAPI bagimliligi: istek otonom-key veya per-device-key ile auth
    olduysa ZORLANACAK from_device'i dondur; master-key -> '' (body-from_device korunur,
    legacy/unverified). Unforgeable-genellemesi (P0): hangi key authenticate ettiyse kimlik
    ODUR — #100526 kimlik-karismasi sinifinin yapisal cozumu."""
    if _is_autonomous_key(x_memory_key):
        return AUTONOMOUS_FROM_DEVICE
    return _resolve_device_key(x_memory_key)


router = APIRouter(prefix="/api/v1/memory", tags=["memory"], dependencies=[Depends(verify_key)])
# Onboarding endpoints embed MEMORY_API_KEY in their response prompts (so a
# bootstrapped Claude instance has the auth header it needs). They MUST require
# the key on the request side too — otherwise anyone reachable on the LAN /
# Tailscale can curl /onboard/<device> and pull the live API key out of the
# response body.
public_router = APIRouter(prefix="/api/v1/memory", tags=["memory-public"], dependencies=[Depends(verify_master_key)])


def get_db() -> Any:
    # Kanonik data_layer'a delege (tek-kaynak: busy_timeout=5000 + WAL + Row).
    # Eskiden inline'dı; lock-flap dersi (server.db corruption→45 spam) artık tek yerde.
    return get_conn(DB_PATH)


_read_by_ready = False


def _ensure_read_by(db: Any) -> None:
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


_verified_ready = False


def _ensure_verified(db: Any) -> None:
    """notes.verified kolonunu idempotent ekle (P0 kimlik): 1 = from_device sunucu-tarafinda
    key'den turetildi (unforgeable), 0 = legacy master-key yazimi (body-iddiasi, dogrulanmamis).
    Gecmis kayitlar NULL/0 kalir — durustce 'unverified' (gecmisi yeniden-etiketleme YOK)."""
    global _verified_ready
    if _verified_ready:
        return
    try:
        cols = [r[1] for r in db.execute("PRAGMA table_info(notes)").fetchall()]
        if "verified" not in cols:
            db.execute("ALTER TABLE notes ADD COLUMN verified INTEGER DEFAULT 0")
            db.commit()
    except Exception:
        pass
    _verified_ready = True


_status_ready = False


def _ensure_status(db: Any) -> None:
    """notes.status kolonunu idempotent ekle (policy-gate #1222 — cross-agent dispatch HOLD).
    Degerler: 'active' (default, teslim-edilebilir) / 'held' (otonom-suspicious, insan-onayi bekler,
    aliciya teslim YOK) / 'rejected' (kalici-held, audit durur). Mevcut satirlar NULL -> COALESCE ile
    'active' muamelesi (geri-uyum; gate-OFF davranisi degismez). _ensure_read_by ile ayni idempotent-desen."""
    global _status_ready
    if _status_ready:
        return
    try:
        cols = [r[1] for r in db.execute("PRAGMA table_info(notes)").fetchall()]
        if "status" not in cols:
            db.execute("ALTER TABLE notes ADD COLUMN status TEXT DEFAULT 'active'")
            db.commit()
    except Exception:
        pass
    _status_ready = True


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


async def _fire_event(event: str, payload: dict[str, Any]):
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


# _track_read'in {table} f-string'i SQL'e gömülüyor. Şu an tüm çağrılar
# hardcoded literal (exploit yok) ama savunma-derinliği: değer her zaman
# bu allowlist'ten gelsin, gelecekte user-input sızması imkânsız olsun.
_READ_TRACK_TABLES = frozenset({"memories", "discoveries"})


def _track_read(db, table: str, row_id: int):
    """Read tracking — her okumada sayaç artır"""
    if table not in _READ_TRACK_TABLES:
        raise ValueError(f"Invalid read-tracking table: {table!r}")
    db.execute(f"UPDATE {table} SET read_count=read_count+1, last_read_at=datetime('now') WHERE id=?", (row_id,))  # noqa: S608 (table allowlist-doğrulamalı)
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
    tasks_completed: list[Any] | None = None
    files_changed: list[Any] | None = None
    bugs_found: list[Any] | None = None
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
    files_changed: list[Any] | None = None
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
    # Codex#176: tekrarlayan-LOG kayıtları (ör. haftalık ajan-sağlık raporu) semantic-dedup'ı
    # ATLAMALI — ardışık raporlar cosine≥0.90 (0.972 ölçüldü) → dedup onları MERGE eder, hafta-unique
    # başlık yetmez, geçmiş kaybolur. skip_dedup=True → semantic-dedup atla (exact-title yine korur).
    skip_dedup: bool = False

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
    rationale: str | None = None

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


# ── Domain router submodule'leri (Faz 3) ──
# Import = handler'ların router'a kaydı + app.api.memory.* re-export'u.
# Kernel (DB_PATH/keys/verify_key/get_db/router'lar/helpers/models) yukarıda kalır.
from app.api.memory import dashboard as dashboard  # noqa: E402, F401
from app.api.memory import devices as devices  # noqa: E402, F401
from app.api.memory import discoveries as discoveries  # noqa: E402, F401
from app.api.memory import health as health  # noqa: E402, F401
from app.api.memory import memories as memories  # noqa: E402, F401
from app.api.memory import misc as misc  # noqa: E402, F401
from app.api.memory import notes as notes  # noqa: E402, F401
from app.api.memory import onboard as onboard  # noqa: E402, F401
from app.api.memory import sessions as sessions  # noqa: E402, F401
from app.api.memory import tasks as tasks  # noqa: E402, F401

# app/api/security.py bu 3 discovery handler'ını FONKSİYON olarak yeniden kullanır
# (pentest findings = type=bug discovery) → app.api.memory.<name> attribute'u korunmalı.
from app.api.memory.discoveries import (  # noqa: E402, F401
    get_discovery as get_discovery,
)
from app.api.memory.discoveries import (
    list_discoveries as list_discoveries,
)
from app.api.memory.discoveries import (
    resolve_discovery as resolve_discovery,
)
