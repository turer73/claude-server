"""Async SQLite database with schema migration."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import aiosqlite

from app.core.db_health_alarm import report_db_failure, report_db_recovered

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Kalıcı bağlantının "zehirlendiği" durumlar: dosya diskte yazılabilir olsa bile
# bu bağlantı bir daha çalışmaz, tek çare yeniden bağlanmak. 2026-08-18'de tam
# olarak bu yaşandı — cron'un kısa-ömürlü sqlite3 CLI'ı aynı dosyaya 8 gün
# boyunca sorunsuz yazdı, uygulamanın kalıcı bağlantısı ise ölü kaldı.
#
# DAR TUTULDU (bilerek): "database is locked" (busy_timeout'un işi),
# "no such table" (şema hatası) ve IntegrityError (kısıt ihlali) BURAYA GİRMEZ —
# onlarda reconnect ya faydasız ya da gerçek bir bug'ı maskeler.
_RECONNECT_MARKERS = (
    "file is not a database",
    "database disk image is malformed",
)


def _is_connection_poisoned(exc: sqlite3.DatabaseError) -> bool:
    if isinstance(exc, sqlite3.IntegrityError):
        return False
    msg = str(exc).lower()
    return any(marker in msg for marker in _RECONNECT_MARKERS)


# DB path fallback'i için TEK kaynak. Production systemd DB_PATH set eder; bu
# yalnızca env yokken devreye girer. main.py (schema init) ve events.py (emit/read)
# AYNI değeri kullanmalı — yoksa events farklı/tablosuz path'e yazıp sessiz drop olur.
from app.db.data_layer import DEFAULT_DB_PATH  # tek-kaynak data_layer (re-export; mevcut import-yollari korunur)

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    permissions TEXT NOT NULL DEFAULT 'read',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_used TEXT,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    request_id TEXT NOT NULL,
    user TEXT NOT NULL,
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    details TEXT,
    status TEXT NOT NULL,
    ip_address TEXT
);

CREATE TABLE IF NOT EXISTS metrics_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    cpu_usage REAL,
    memory_usage REAL,
    disk_usage REAL,
    temperature REAL,
    load_avg TEXT,
    network_io TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    severity TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL,
    resolved INTEGER DEFAULT 0,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS vps_metrics_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    online INTEGER NOT NULL DEFAULT 1,
    cpu_usage REAL,
    memory_usage REAL,
    disk_usage REAL,
    containers_total INTEGER,
    containers_up INTEGER
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user);
CREATE INDEX IF NOT EXISTS idx_metrics_timestamp ON metrics_history(timestamp);
CREATE INDEX IF NOT EXISTS idx_vps_metrics_timestamp ON vps_metrics_history(timestamp);
-- Expression index: zaman-pencere sorgusu format-agnostik datetime(timestamp) ile
-- filtrelenir (ISO-T + boşluk-default karışımını UTC'ye normalize eder). Bu index o
-- normalize-predicate'e karşı RANGE-SEARCH sağlar (Codex P2: aksi halde pencere<500
-- satırda full index-SCAN). Bkz devops_agent.get_metrics_history.
CREATE INDEX IF NOT EXISTS idx_metrics_dt ON metrics_history(datetime(timestamp));
CREATE INDEX IF NOT EXISTS idx_vps_metrics_dt ON vps_metrics_history(datetime(timestamp));
CREATE INDEX IF NOT EXISTS idx_alerts_severity ON alerts(severity);

CREATE TABLE IF NOT EXISTS ci_lesson_learned (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_uuid TEXT NOT NULL,
    project TEXT NOT NULL,
    test_name TEXT NOT NULL,
    error_hash TEXT NOT NULL,
    signature TEXT NOT NULL,
    raw_error TEXT,
    attempt_num INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    context_lessons TEXT,
    fix_diff TEXT,
    outcome TEXT NOT NULL,
    duration_ms INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_lesson_signature ON ci_lesson_learned(signature, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_lesson_project ON ci_lesson_learned(project, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_lesson_run_uuid ON ci_lesson_learned(run_uuid);

-- LIVESYS Faz 1: cron job GERÇEK outcome'u (rc değil). klipper-cron-wrap.sh yazar.
-- "koştu-ama-kötü" sinyali; Uptime-Kuma dead-man's-switch'i ("hiç koşmadı") REPLACE etmez, tamamlar.
CREATE TABLE IF NOT EXISTS cron_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    job TEXT NOT NULL,
    result TEXT NOT NULL,            -- pass | partial | fail
    rc INTEGER,
    source TEXT NOT NULL,            -- predicate | rc-fallback | outcome-rc-mismatch | undefined
    detail TEXT,
    attempt_no INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_cron_outcomes_job ON cron_outcomes(job, timestamp DESC);

-- LIVESYS Faz 3.2: hafif olay omurgası. Dağınık olay-üreticileri (cron_outcomes,
-- liveness, pr-review, alerts, deploy/fix) TEK merkezi kayda route eder; digest+
-- alert okur; severity>=warn deterministik bildirim (Claude-heartbeat DEĞİL).
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    type TEXT NOT NULL,              -- job-outcome | liveness | pr-event | alert | deploy | fix | backup | ...
    source TEXT NOT NULL,            -- üretici (örn. cron:demo-reset, liveness:rag, pr:claude-server#16)
    severity TEXT NOT NULL DEFAULT 'info',  -- info | warn | critical
    title TEXT NOT NULL,
    detail TEXT,
    payload TEXT,                    -- opsiyonel JSON
    notified INTEGER NOT NULL DEFAULT 0,    -- bildirim gönderildi mi (idempotent)
    acked INTEGER NOT NULL DEFAULT 0        -- kullanıcı Telegram '✅ Gördüm' ile onayladı mı (escalation durur)
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_events_sev ON events(severity, notified, timestamp DESC);

-- LIVESYS Faz 5 (kapalı-döngü otonomi) Slice-1: kalıcı remediation ledger.
-- devops_agent her remediation girişimini (yürütülen VEYA mode!=auto'da niyet)
-- buraya yazar (in-memory deque yerine kalıcı audit). verify_status/escalated
-- sonraki slice'lar için (verify→rollback/escalate); şimdilik NULL.
CREATE TABLE IF NOT EXISTS remediation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    alert_source TEXT NOT NULL,      -- cpu | memory | disk | temperature | service:<x> | docker:<x>
    severity TEXT,
    mode TEXT NOT NULL,              -- notify | dry_run | auto (config.remediation_mode)
    action TEXT,                     -- playbook adım açıklaması
    command TEXT,                    -- planlanan/yürütülen komut
    executed INTEGER NOT NULL DEFAULT 0,    -- 1 = gerçekten çalıştı (mode=auto), 0 = niyet/skip
    result TEXT,                     -- stdout/err (executed) veya 'skipped: mode=<m>'
    success INTEGER,                 -- exec başarılı mı (executed=1 iken); NULL = uygulanmadı
    verify_status TEXT,              -- FAZ5-S2: post-action doğrulama (NULL şimdilik)
    escalated INTEGER NOT NULL DEFAULT 0,   -- FAZ5-S2: eskale edildi mi
    rolled_back INTEGER NOT NULL DEFAULT 0, -- INTERV: verify-fail sonrası geri-alındı mı (yalnız reversible)
    rollback_result TEXT,            -- INTERV: rollback komut çıktısı
    provenance TEXT                  -- INTERV: tetik-kökeni JSON (build_provenance)
);

CREATE INDEX IF NOT EXISTS idx_remediation_ts ON remediation_log(timestamp DESC);
"""


class Database:
    """Async SQLite database wrapper."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._reconnect_lock = asyncio.Lock()
        # Eşzamanlı çağrıların aynı arızada üst üste reconnect denemesini önler:
        # kilidi alan ilk coroutine bağlantıyı tazeler, kuyruktakiler jenerasyon
        # numarasının değiştiğini görüp kendi denemelerini atlar.
        self._generation = 0

    async def initialize(self) -> None:
        # DB-sertleştirme (audit P1#7): prod'da DB_PATH set edilmezse /tmp fallback'e
        # SESSİZCE düşmek = veri-kaybı (events başka path'e yazar, restart'ta /tmp uçar).
        # pytest dışında /tmp-default tespit edilirse GÖRÜNÜR uyar (sessiz değil).
        if self.db_path == DEFAULT_DB_PATH and not os.environ.get("PYTEST_CURRENT_TEST"):
            logger.warning(
                "DB /tmp fallback kullanılıyor (%s) — DB_PATH set edilmemiş! Prod'da "
                "veri-kaybı riski (restart'ta /tmp uçabilir). systemd DB_PATH'i doğrula.",
                self.db_path,
            )
        self._conn = await self._open()
        await self._conn.executescript(SCHEMA_V1)
        await self._migrate()
        await self._conn.commit()

    async def _open(self) -> aiosqlite.Connection:
        """Bağlantı + bağlantı-başı PRAGMA'lar. initialize() ve _reconnect() ORTAK kullanır.

        Ortak olması şart: reconnect bu PRAGMA'ları atlarsa yeni bağlantı
        busy_timeout'suz kalır ve "database is locked" hataları geri döner.
        """
        conn = await aiosqlite.connect(self.db_path)
        conn.row_factory = aiosqlite.Row
        # DB-sertleştirme (audit P1#6): WAL = eşzamanlı okuma+yazma (uvicorn 2-worker +
        # events.py ikili-writer → #517 kilit-çekişmesinin kökü). busy_timeout = kilitliyse
        # "database is locked" yerine N ms bekle (BUSY≠READONLY≠hata). journal_mode=WAL
        # DB-düzeyinde kalıcı; busy_timeout bağlantı-başı → her worker initialize'da set eder.
        # Codex P2: busy_timeout WAL'DEN ÖNCE — DELETE→WAL geçişi kilit alır; başka writer
        # (cron/worker) kilidi tutuyorsa WAL pragma'sı bekleyecek olan ilk işlem, timeout
        # daha kurulmadan "database is locked" atabilir. Önce timeout, sonra kilit-alan WAL.
        await conn.execute("PRAGMA busy_timeout=10000")
        await conn.execute("PRAGMA journal_mode=WAL")
        return conn

    async def _reconnect(self, seen_generation: int) -> bool:
        """Zehirlenmiş bağlantıyı tazele. Başarılıysa True.

        seen_generation: çağıranın hatayı gördüğü andaki jenerasyon. Kilit
        içinde jenerasyon değişmişse başka bir coroutine zaten tazelemiştir —
        tekrar bağlanma, mevcut bağlantıyla devam et.
        """
        async with self._reconnect_lock:
            if self._generation != seen_generation:
                return True

            # ÖNCE yeni bağlantıyı kur, SONRA takas et. `self._conn`'u arada None
            # yapmak, hatayı görüp kilidi bekleyen eşzamanlı çağrıların
            # "Database not initialized" ile patlamasına yol açıyordu (testle yakalandı).
            new_conn = await self._open()
            old, self._conn = self._conn, new_conn
            self._generation += 1

            if old is not None:
                try:
                    await old.close()
                except Exception as exc:  # bozuk bağlantıda close da patlayabilir
                    logger.warning("bozuk DB bağlantısı kapatılamadı (yok sayılıyor): %s", exc)
            return True

    async def _with_recovery(self, op: str, fn: Callable[[], Awaitable[T]]) -> T:
        """fn()'i çalıştır; bağlantı zehirlendiyse BİR KEZ yeniden bağlanıp tekrar dene.

        Tek deneme bilinçli: kalıcı bozulmada sonsuz retry, 55.855 hatalık
        sessiz kesintinin yerine 55.855 reconnect fırtınası koyardı.
        """
        generation = self._generation
        try:
            return await fn()
        except sqlite3.DatabaseError as exc:
            if not _is_connection_poisoned(exc):
                raise
            logger.error("DB bağlantısı zehirlendi (%s): %s — yeniden bağlanılıyor", op, exc)
            try:
                await self._reconnect(generation)
            except Exception as reconnect_exc:
                logger.critical("DB yeniden bağlanma BAŞARISIZ (%s): %s", op, reconnect_exc)
                report_db_failure(op, reconnect_exc)
                raise exc from reconnect_exc

            try:
                result = await fn()
            except sqlite3.DatabaseError as retry_exc:
                logger.critical("DB yeniden bağlandı ama işlem yine başarısız (%s): %s", op, retry_exc)
                report_db_failure(op, retry_exc)
                raise

            logger.warning("DB bağlantısı yeniden kuruldu, işlem başarılı (%s)", op)
            report_db_recovered(op)
            return result

    async def _migrate(self) -> None:
        """İdempotent kolon-eklemeleri: CREATE TABLE IF NOT EXISTS mevcut (prod)
        tabloya yeni kolon EKLEMEZ -> ALTER ile ekle (yoksa). Fresh-db'de SCHEMA_V1
        zaten içerir -> atlanır."""
        cur = await self._conn.execute("PRAGMA table_info(events)")
        cols = {row[1] for row in await cur.fetchall()}
        if cols and "acked" not in cols:
            await self._add_column("events", "acked", "INTEGER NOT NULL DEFAULT 0")

        # INTERV: remediation_log'a rollback + provenance kolonları (idempotent)
        cur = await self._conn.execute("PRAGMA table_info(remediation_log)")
        rcols = {row[1] for row in await cur.fetchall()}
        if rcols:
            if "rolled_back" not in rcols:
                await self._add_column("remediation_log", "rolled_back", "INTEGER NOT NULL DEFAULT 0")
            if "rollback_result" not in rcols:
                await self._add_column("remediation_log", "rollback_result", "TEXT")
            if "provenance" not in rcols:
                await self._add_column("remediation_log", "provenance", "TEXT")

        # SİNYAL-BÜTÜNLÜĞÜ: alerts'e bi-temporal kolonlar (discoveries ile aynı ilke).
        # transaction-time (resolved/resolved_at) ZATEN var; VALID-time ekliyoruz.
        cur = await self._conn.execute("PRAGMA table_info(alerts)")
        acols = {row[1] for row in await cur.fetchall()}
        if acols:
            if "valid_at" not in acols:
                await self._add_column("alerts", "valid_at", "TEXT")
                await self._conn.execute("UPDATE alerts SET valid_at = timestamp WHERE valid_at IS NULL")
            if "invalid_at" not in acols:
                await self._add_column("alerts", "invalid_at", "TEXT")
                # backfill: zaten resolved olanların gerçek-dünya geçersizliği = resolved_at
                await self._conn.execute("UPDATE alerts SET invalid_at = resolved_at WHERE invalid_at IS NULL AND resolved=1")
            if "supersedes_id" not in acols:
                await self._add_column("alerts", "supersedes_id", "INTEGER")

    async def _add_column(self, table: str, column: str, decl: str) -> None:
        """Race-safe ALTER TABLE ADD COLUMN.

        2 uvicorn worker ayni anda fresh-DB'de _migrate kosunca ikisi de
        PRAGMA table_info'da kolonu yok gorur, ikisi de ALTER atar; kaybeden
        worker "duplicate column name" (sqlite3.OperationalError) alir. Bunu
        idempotent kabul et (kolon artik var) - diger hatalar yukari firlar.
        """
        try:
            await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        return self._conn

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Cursor:
        async def _run() -> aiosqlite.Cursor:
            cursor = await self.conn.execute(sql, params)
            await self.conn.commit()
            return cursor

        # Retry güvenli: hata commit'ten ÖNCE atılırsa hiçbir şey kalıcı olmamıştır,
        # dolayısıyla ikinci deneme yazmayı ÇİFTLEMEZ.
        return await self._with_recovery("execute", _run)

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        async def _run() -> list[dict[str, Any]]:
            cursor = await self.conn.execute(sql, params)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

        return await self._with_recovery("fetch_all", _run)

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        async def _run() -> dict[str, Any] | None:
            cursor = await self.conn.execute(sql, params)
            row = await cursor.fetchone()
            return dict(row) if row else None

        return await self._with_recovery("fetch_one", _run)
