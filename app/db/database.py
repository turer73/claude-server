"""Async SQLite database with schema migration."""

from __future__ import annotations

import logging
import os

import aiosqlite

logger = logging.getLogger(__name__)

# DB path fallback'i için TEK kaynak. Production systemd DB_PATH set eder; bu
# yalnızca env yokken devreye girer. main.py (schema init) ve events.py (emit/read)
# AYNI değeri kullanmalı — yoksa events farklı/tablosuz path'e yazıp sessiz drop olur.
DEFAULT_DB_PATH = "/tmp/linux-ai-server-test.db"  # noqa: S108 — kasıtlı fallback; prod DB_PATH override eder

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
    escalated INTEGER NOT NULL DEFAULT 0    -- FAZ5-S2: eskale edildi mi
);

CREATE INDEX IF NOT EXISTS idx_remediation_ts ON remediation_log(timestamp DESC);
"""


class Database:
    """Async SQLite database wrapper."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

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
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        # DB-sertleştirme (audit P1#6): WAL = eşzamanlı okuma+yazma (uvicorn 2-worker +
        # events.py ikili-writer → #517 kilit-çekişmesinin kökü). busy_timeout = kilitliyse
        # "database is locked" yerine N ms bekle (BUSY≠READONLY≠hata). journal_mode=WAL
        # DB-düzeyinde kalıcı; busy_timeout bağlantı-başı → her worker initialize'da set eder.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=10000")
        await self._conn.executescript(SCHEMA_V1)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        """İdempotent kolon-eklemeleri: CREATE TABLE IF NOT EXISTS mevcut (prod)
        tabloya yeni kolon EKLEMEZ -> ALTER ile ekle (yoksa). Fresh-db'de SCHEMA_V1
        zaten içerir -> atlanır."""
        cur = await self._conn.execute("PRAGMA table_info(events)")
        cols = {row[1] for row in await cur.fetchall()}
        if cols and "acked" not in cols:
            await self._conn.execute("ALTER TABLE events ADD COLUMN acked INTEGER NOT NULL DEFAULT 0")

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        return self._conn

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        cursor = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cursor

    async def fetch_all(self, sql: str, params: tuple = ()) -> list[dict]:
        cursor = await self.conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def fetch_one(self, sql: str, params: tuple = ()) -> dict | None:
        cursor = await self.conn.execute(sql, params)
        row = await cursor.fetchone()
        return dict(row) if row else None
