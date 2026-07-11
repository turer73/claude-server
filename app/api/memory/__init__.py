"""
Claude Memory API v2 — Merkezi hafıza sistemi
Duplicate koruması, FTS arama, read tracking, lifecycle yönetimi.
"""

import asyncio
import hashlib
import re
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, field_validator

from app.core.config import read_env_var
from app.db.data_layer import MEMORY_DB, get_conn

DB_PATH = MEMORY_DB

MEMORY_API_KEY = read_env_var("MEMORY_API_KEY")
# GAP-1 item-D (#1222 A-2): DISTINCT otonom-key. Otonom-spawn bu key ile auth olur;
# create_note from_device'i 'klipper-autonomous'a ZORLA-override eder (unforgeable —
# spawn body'de ne derse desin). Set edilmemisse ozellik dormant (geriye-uyumlu).
MEMORY_API_KEY_AUTONOMOUS = read_env_var("MEMORY_API_KEY_AUTONOMOUS")
# P0-P1fix (#100564 Codex-P1): mint/revoke icin master'dan AYRI admin-key. Master su an
# HERKESIN gunluk-credential'i (onboard-prompt gomuyor) -> master-only koruma iluzyon:
# herhangi bir ajan digerinin key'ini rotate edebilirdi. Admin-key YALNIZ Turgut/operator'de
# kalir, hicbir ajanin gunluk-credential'i olmaz. Set edilmemisse dormant (geriye-uyum,
# autonomous-key gecis-deseni); master==admin config-hatasi = dormant (collision-guard).
MEMORY_API_KEY_ADMIN = read_env_var("MEMORY_API_KEY_ADMIN")
AUTONOMOUS_FROM_DEVICE = "klipper-autonomous"

VALID_DISCOVERY_TYPES = ("bug", "fix", "learning", "config", "workaround", "architecture", "plan")
VALID_STATUSES = ("active", "completed", "obsolete", "superseded")
TRASH_TITLES = re.compile(r"^(test|test bug|test fix|test workaround|deneme|asdf|xxx)$", re.IGNORECASE)


def _admin_key_active() -> bool:
    """Admin-key devrede mi: set + master'dan VE otonom'dan distinct. admin==master VEYA
    admin==otonom config-hatasi = dormant (collision-guard) — yoksa otonom-spawn surecleri
    (insan degil) verify_admin_key'i gecip mint/rotate/revoke yapabilirdi (Codex#302-2tur #4)."""
    return bool(MEMORY_API_KEY_ADMIN) and MEMORY_API_KEY_ADMIN != MEMORY_API_KEY and MEMORY_API_KEY_ADMIN != MEMORY_API_KEY_AUTONOMOUS


def _is_admin_key(x_memory_key: str | None) -> bool:
    """DISTINCT admin-key ile mi auth oldu (mint/revoke idaresi). Bos-key asla admin."""
    return _admin_key_active() and x_memory_key == MEMORY_API_KEY_ADMIN


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
        # Codex#302-P2: flag YALNIZ basarida — transient-lock'ta eski hali 'hazir' deyip
        # process-yasam-boyu (restart'a dek) mint-500/auth-401 kilitliyordu; simdi retry eder
        _device_keys_ready = True
    except Exception:
        pass


def _resolve_device_key(x_memory_key: str | None) -> str:
    """Key aktif bir device-key ise cihaz adini dondur, degilse ''.
    Master/otonom-key burada COZULMEZ (oncelik verify_key/dispatch_origin'de) — master
    string'i yanlislikla device-key yapilirsa master-yolu kazanir (fail-safe, collision-guard
    deseniyle tutarli). Her cagride SELECT: lokal SQLite, cache-invalidation karmasasina degmez."""
    if not x_memory_key or x_memory_key == MEMORY_API_KEY or _is_admin_key(x_memory_key) or _is_autonomous_key(x_memory_key):
        return ""
    key_hash = hashlib.sha256(x_memory_key.encode()).hexdigest()
    try:
        db = get_db()
        try:
            _ensure_device_keys(db)
            row = db.execute("SELECT device FROM device_keys WHERE key_hash=? AND active=1", (key_hash,)).fetchone()
            if row:
                # Codex#302-P2: telemetri-UPDATE'i best-effort — lock/race'te kimlik DUSMESIN
                # (eski hali '' donup create_note'u master-legacy/spoof'a dusurebiliyordu)
                try:
                    db.execute("UPDATE device_keys SET last_used_at=datetime('now') WHERE key_hash=?", (key_hash,))
                    db.commit()
                except Exception:
                    pass
                return str(row[0])
        finally:
            db.close()
    except Exception as e:
        raise HTTPException(503, "device_keys erisilemedi (transient) — yeniden dene") from e
    return ""


def verify_key(x_memory_key: str = Header(None)) -> None:
    # FAIL-CLOSED (güvenlik fix): MEMORY_API_KEY yüklenmemişse erişimi AÇMA.
    # Eski 'if KEY and ...' boş-key'de 401 atmıyordu -> env-yükleme hatasında
    # memory/RAG/research/classifier tamamen korumasız kalıyordu.
    # SCOPE (Codex#302-2tur #3, Turgut karari): device-key BURADA KABUL EDILMEZ —
    # bu dependency'yi dispatch/research/rag/classifier/prometheus/ws_status da import
    # ediyor; notes-koordinasyonu icin uretilmis bir device-key dispatch/task gibi
    # gercek-aksiyon endpoint'lerini ACMAMALI. Device-key'ler yalniz memory router'inin
    # verify_key_memory_scoped'inde gecerli.
    if not MEMORY_API_KEY:
        raise HTTPException(503, "Memory API key not configured (fail-closed)")
    if x_memory_key == MEMORY_API_KEY or _is_admin_key(x_memory_key) or _is_autonomous_key(x_memory_key):
        return
    raise HTTPException(401, "Invalid memory API key")


# Codex#302-3tur (klipper #100579 mimari-oneri): DEFAULT-DENY + route-allowlist.
# Router-seviyesi genel-kabul her review-turunda yeni alt-kapsam aciyordu (notes ->
# read-tracking -> maintenance -> memories/devices). Device-key yalniz asagida ACIKCA
# izin verilen (method, path) ciftlerini acar; YENI route'lar otomatik master/admin-only
# dogar (whack-a-mole yapisal biter). Kural: yazma-route'u ancak kimligi forced-origin
# ile KEY'den turetiyorsa listeye girer. NOT: 303 (claims) ve 305 (discussions) rebase'te
# kendi route'larini eklemeli.
DEVICE_KEY_ROUTE_ALLOWLIST: frozenset = frozenset(
    {
        # koordinasyon (forced-origin'li yazim + inbox okuma)
        ("GET", "/api/v1/memory/notes"),
        ("POST", "/api/v1/memory/notes"),
        ("PUT", "/api/v1/memory/notes/{note_id}/read"),
        # zorunlu-kayit akislari (forced-origin'li)
        ("GET", "/api/v1/memory/sessions"),
        ("GET", "/api/v1/memory/sessions/{session_id}"),
        ("POST", "/api/v1/memory/sessions"),
        ("GET", "/api/v1/memory/tasks"),
        ("POST", "/api/v1/memory/tasks"),
        ("PATCH", "/api/v1/memory/tasks/{task_id}"),
        # Codex#302-4tur: discoveries/search/dashboard/devices/device-projects/projects GET'i
        # BURADAN CIKARILDI — hepsi unscoped-global sorgu (caller-kimligine gore filtrelemiyor),
        # bir device-key TUM cihazlarin discovery/task-gecmisi/local-path/hostname/IP'sini
        # gorebiliyordu. Bu route'lar P0'in amaci (yazi-provenance) icin GEREKMIYOR — master/
        # admin-only'e donduruldu (default-deny: ihtiyac-kanitlanmadan acilmaz).
        ("POST", "/api/v1/memory/discoveries"),
        ("PUT", "/api/v1/memory/discoveries/{discovery_id}"),
        ("PUT", "/api/v1/memory/discoveries/{discovery_id}/resolve"),
        ("GET", "/api/v1/memory/memories"),
        ("GET", "/api/v1/memory/memories/{memory_id}"),
        ("POST", "/api/v1/memory/memories"),
        ("GET", "/api/v1/memory/surface"),
        ("GET", "/api/v1/memory/world-model"),
        ("GET", "/api/v1/memory/health"),
        # CLAIM-lock (PR#303, sahiplik kimlik-key'den — forced-origin'li)
        ("POST", "/api/v1/memory/claims"),
        ("PUT", "/api/v1/memory/claims/{claim_id}/release"),
        ("PUT", "/api/v1/memory/claims/{claim_id}/renew"),
        ("GET", "/api/v1/memory/claims"),
        # tartisma-platformu (PR#305, kimlik-key'den — decide KASITLI DISI birakildi,
        # zaten kendi verify_master_key'i var, default-deny onu ayrica korur)
        ("POST", "/api/v1/memory/discussions"),
        ("GET", "/api/v1/memory/discussions"),
        ("POST", "/api/v1/memory/discussions/{topic_id}/positions"),
        ("GET", "/api/v1/memory/discussions/{topic_id}/positions"),
        ("POST", "/api/v1/memory/discussions/{topic_id}/synthesize"),
    }
)


def verify_key_memory_scoped(request: Request, x_memory_key: str = Header(None)) -> None:
    """Memory router'a OZEL auth: master/admin/otonom her route; device-key YALNIZ
    DEVICE_KEY_ROUTE_ALLOWLIST'teki (method, path) — geri kalan her sey 403 (default-deny).
    Baska router bu dependency'yi KULLANMAMALI (device-key'in tek yetki-alani burasi)."""
    if not MEMORY_API_KEY:
        raise HTTPException(503, "Memory API key not configured (fail-closed)")
    if x_memory_key == MEMORY_API_KEY or _is_admin_key(x_memory_key) or _is_autonomous_key(x_memory_key):
        return
    if _resolve_device_key(x_memory_key):
        route = request.scope.get("route")
        path_fmt = getattr(route, "path_format", None) or getattr(route, "path", "")
        if (request.method, path_fmt) in DEVICE_KEY_ROUTE_ALLOWLIST:
            return
        raise HTTPException(403, "Device-key bu route'ta yetkili degil (default-deny; master/admin gerekli)")
    raise HTTPException(401, "Invalid memory API key")


def _origin_str(forced_origin: Any) -> str:
    """dispatch_origin sonucunu normalize et. FastAPI DI DISI dogrudan cagrilarda (orn.
    main.py boot dead-gate emit -> create_discovery) parametre Depends-SENTINEL objesi
    olarak gelir — TRUTHY ama string degil; 'forced_origin or ...' onu kimlik sanip SQL
    bind'i patlatiyordu (Codex#302-3tur #1). String olmayan her sey '' (master-legacy)."""
    return forced_origin if isinstance(forced_origin, str) else ""


def verify_master_key(x_memory_key: str = Header(None)) -> None:
    """MASTER-key ZORUNLU — otonom-key REDDEDILIR (Codex Tier-1 #1). Onboarding/key-SIZAN
    route'lar icin: onboarding yaniti MASTER-key gomuyor; otonom-key bu endpoint'lere erisip
    master'i ogrenip force-tag'i BYPASS etmesin (unforgeable-garanti korunur)."""
    if not MEMORY_API_KEY:
        raise HTTPException(503, "Memory API key not configured (fail-closed)")
    if x_memory_key != MEMORY_API_KEY:
        raise HTTPException(401, "Invalid memory API key (master required)")


def verify_admin_key(x_memory_key: str = Header(None)) -> None:
    """Key-IDARESI (mint/revoke) icin: admin-key set+distinct ise YALNIZ o kabul; dormant'ta
    master (gecis). Ajan device-key'leri ve otonom-key HER DURUMDA reddedilir."""
    if not MEMORY_API_KEY:
        raise HTTPException(503, "Memory API key not configured (fail-closed)")
    if _is_admin_key(x_memory_key):
        return
    # admin-key aktif DEGILSE (unset veya collision-dormant) master gecise izin; aktif ISE
    # master REDDEDILIR. _admin_key_active tek-kaynak: otonom-collision'da da dormant
    # (yoksa mint tamamen kilitlenirdi — master-collision davranisiyla tutarli).
    if not _admin_key_active() and x_memory_key == MEMORY_API_KEY:
        return
    raise HTTPException(401, "Key-idaresi icin ADMIN key gerekli (master su an herkesin credential'i)")


def dispatch_origin(x_memory_key: str = Header(None)) -> str:
    """create_note icin FastAPI bagimliligi: istek otonom-key veya per-device-key ile auth
    olduysa ZORLANACAK from_device'i dondur; master-key -> '' (body-from_device korunur,
    legacy/unverified). Unforgeable-genellemesi (P0): hangi key authenticate ettiyse kimlik
    ODUR — #100526 kimlik-karismasi sinifinin yapisal cozumu."""
    if _is_autonomous_key(x_memory_key):
        return AUTONOMOUS_FROM_DEVICE
    if not x_memory_key or x_memory_key == MEMORY_API_KEY or _is_admin_key(x_memory_key):
        return ""  # master/admin-legacy: body korunur (verified=0)
    device = _resolve_device_key(x_memory_key)
    if not device:
        # verify_key'den GECMIS ama cozulemiyor (revoke-race/transient) — Codex#302-P2:
        # master-legacy'ye DUSME (spoof-yuzeyi), fail-closed
        raise HTTPException(401, "Device-key cozulemedi (revoked/transient) — yeniden dene")
    return device


router = APIRouter(prefix="/api/v1/memory", tags=["memory"], dependencies=[Depends(verify_key_memory_scoped)])
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
        # ayni flag-yalniz-basarida deseni (Codex#302-2tur #2 sinifi, proaktif)
        _read_by_ready = True
    except Exception:
        pass


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
        # Codex#302-2tur #2: flag YALNIZ basarida (_ensure_device_keys deseni) — transient
        # ALTER-hatasinda 'hazir' denirse INSERT(...verified) restart'a dek her yazimda patlar
        _verified_ready = True
    except Exception:
        pass


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
        # ayni flag-yalniz-basarida deseni (Codex#302-2tur #2 sinifi, proaktif)
        _status_ready = True
    except Exception:
        pass


_thread_fields_ready = False


def _ensure_thread_fields(db: Any) -> None:
    """notes.thread_id/reply_to/hop_count/msg_type kolonlarini idempotent ekle (Faz-A,
    docs/autonomous-comms-design.md §2 — otonom-haberlesme genisletme, #100447 12-madde
    mutabakat). Mevcut satirlar NULL/0/'legacy' kalir (backward-tolerant; eski-istemci
    yeni-alansiz POST /notes calismaya devam eder, server-default uygular).
    _ensure_read_by/_ensure_status ile AYNI idempotent-desen."""
    global _thread_fields_ready
    if _thread_fields_ready:
        return
    try:
        cols = [r[1] for r in db.execute("PRAGMA table_info(notes)").fetchall()]
        if "thread_id" not in cols:
            db.execute("ALTER TABLE notes ADD COLUMN thread_id INTEGER")
            db.commit()
        if "reply_to" not in cols:
            db.execute("ALTER TABLE notes ADD COLUMN reply_to INTEGER")
            db.commit()
        if "hop_count" not in cols:
            db.execute("ALTER TABLE notes ADD COLUMN hop_count INTEGER DEFAULT 0")
            db.commit()
        if "msg_type" not in cols:
            db.execute("ALTER TABLE notes ADD COLUMN msg_type TEXT DEFAULT 'legacy'")
            db.commit()
        # ayni flag-yalniz-basarida deseni (Codex#302-2tur #2 sinifi, proaktif)
        _thread_fields_ready = True
    except Exception:
        pass


def _ensure_comms_audit_table(db: Any) -> None:
    """autonomous_comms_audit tablosunu idempotent kur (Faz-A §10 — SUBSTRAT). APPEND-ONLY:
    hicbir kod-yolu UPDATE/DELETE yapmaz (yalniz bu modul INSERT eder) — spawn/agent
    edit/silemez. Diger her-sey (kill-switch denetim-izi, shadow-precision, provenans)
    buradan okunur/buraya yazar."""
    db.execute(
        """CREATE TABLE IF NOT EXISTS autonomous_comms_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id INTEGER,
            note_id INTEGER,
            device TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_comms_audit_thread ON autonomous_comms_audit(thread_id)")
    db.commit()


def _ensure_comms_halt_table(db: Any) -> None:
    """autonomous_comms_halt tablosunu idempotent kur (Faz-A §5 — kill-switch katman-1).
    Tek-satir (id=1), poller her-tick spawn-ONCESI okur; active=1 ise spawn atlanir."""
    db.execute(
        """CREATE TABLE IF NOT EXISTS autonomous_comms_halt (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            active INTEGER NOT NULL DEFAULT 0,
            reason TEXT DEFAULT '',
            set_by TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now'))
        )"""
    )
    db.execute("INSERT OR IGNORE INTO autonomous_comms_halt (id, active) VALUES (1, 0)")
    db.commit()


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


class ClaimCreate(BaseModel):
    """P1 CLAIM-lock (konu-1 karari): not-konvansiyonu yerine DB-kisiti.
    task_key = 'repo:kapsam' (ör 'claude-server:memory-api'); repo+branch CI-gate botu icindir."""

    task_key: str
    device: str  # master-key legacy'de body'den; device-key ile auth'ta KEY kazanir (override)
    repo: str | None = None
    branch: str | None = None
    note: str = ""
    ttl_hours: float = 4.0  # CLAIM-protokolu TTL'i

    @field_validator("task_key")
    @classmethod
    def clean_task_key(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3:
            raise ValueError("task_key en az 3 karakter")
        return v

    @field_validator("ttl_hours")
    @classmethod
    def sane_ttl(cls, v: float) -> float:
        if not (0.1 <= v <= 72):
            raise ValueError("ttl_hours 0.1-72 araliginda olmali")
        return v


# Kör-tur koruması (canlı-bulgu #100609, surer'in kendi kullanım-hatası): discussion_topics'in
# title/question alanları GET /discussions listesinde ve open-faz positions-endpoint'inde
# HERKESE görünür — positions tablosunun blind-gate'ine TABİ DEĞİL. Creator buraya katılımcı-
# atıflı sıralama/pozisyon gömerse (ör. "opencode sirasi: 4->3->5") kör-tur delinir: kimse
# yazmadan başkasının pozisyonunu görür. Aşağıdaki validator o sınıfı reddeder. Defense-in-depth
# (regex-denylist, mükemmel-değil — disiplin+kod birlikte, [[regex_denylist_defense_in_depth]]):
# tek-cihaz meta-soruyu GEÇİRİR ("klipper'in önerisi doğru mu?"), yalnız atıflı-sıralama +
# rakam-ok-dizisini KESER.
_LEAK_RANK_ARROW = re.compile(r"\d\s*(?:->|→|=>)\s*\d")
_LEAK_ATTR_POS = re.compile(
    r"(?:surer|klipper|opencode|turgut)[^.\n]{0,25}?(?:sira|pozisyon|görüş|gorus|önceli|onceli)\w*\s*[:=]",
    re.IGNORECASE,
)


def _reject_position_leak(text: str) -> None:
    """title/question kör-tur delen içerik taşıyorsa ValueError. İki sinyal: (a) rakam-ok-dizisi
    (4->3->5 = sıralama-beyanı), (b) cihaz-adı + sıra/pozisyon/görüş + ':' (atıflı-pozisyon)."""
    if _LEAK_RANK_ARROW.search(text) or _LEAK_ATTR_POS.search(text):
        raise ValueError(
            "Acilis-metnine (title/question) katilimci pozisyonu/siralamasi gomme — kor-tur "
            "delinir (herkese gorunur). Pozisyonlar YALNIZ positions-endpoint'ten blind yazilir; "
            "question = yalniz notr soru + baglam."
        )


class DiscussionCreate(BaseModel):
    """Tartisma-platformu MVP (konu-2 sentezi #100561, Turgut onayli)."""

    title: str
    question: str
    device: str  # master-legacy'de body'den; device-key auth'ta KEY kazanir
    expected_devices: str = "turgut,surer,klipper,opencode"

    @field_validator("title", "question")
    @classmethod
    def non_trivial(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 5:
            raise ValueError("en az 5 karakter")
        _reject_position_leak(v)  # kör-tur sızıntı koruması
        return v


class PositionCreate(BaseModel):
    """Zorunlu sablon (4/4 yakinsama): pozisyon/dayanak/guven/beni-ne-ikna-eder/min-1-itiraz.
    out_of_scope opsiyonel (klipper #100554 ek-onerisi, dusuk-guvenli -> zorunlu degil)."""

    device: str
    position: str
    evidence: str
    confidence: int
    persuadable_by: str
    objection: str
    out_of_scope: str = ""

    @field_validator("position", "evidence", "persuadable_by", "objection")
    @classmethod
    def non_empty(cls, v: str) -> str:
        if len(v.strip()) < 10:
            raise ValueError("sablon alani en az 10 karakter — bos-formalite doldurma (sycophancy-panzehiri)")
        return v.strip()

    @field_validator("confidence")
    @classmethod
    def conf_range(cls, v: int) -> int:
        if not (1 <= v <= 10):
            raise ValueError("confidence 1-10")
        return v


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
from app.api.memory import claims as claims  # noqa: E402, F401
from app.api.memory import dashboard as dashboard  # noqa: E402, F401
from app.api.memory import devices as devices  # noqa: E402, F401
from app.api.memory import discoveries as discoveries  # noqa: E402, F401
from app.api.memory import discussions as discussions  # noqa: E402, F401
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
