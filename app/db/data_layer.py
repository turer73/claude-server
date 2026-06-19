"""Kanonik SQLite erişim katmanı — tek path-sabitleri + tek bağlantı-helper'ı.

Neden: claude_memory.db'ye 10+ modülden hardcode-path + 7 farklı bağlantı-deseniyle
erişiliyordu; busy_timeout 5 sınıfa dağılmıştı (bazı server.db YAZICILARI çıplaktı →
gerçek lock-flap riski; server.db corruption → 45-Telegram-spam dersi). Bu modül TEK
sözleşme verir: HER bağlantıda busy_timeout (kilitliyse hata yerine bekle).

`get_conn` kontratı `app.api.memory.get_db` ile aynı: busy_timeout + WAL + Row.
get_db artık buna delege eder (tek-kaynak). server.db env-override (DB_PATH) korunur.
"""

from __future__ import annotations

import os
import sqlite3

from app.db.database import DEFAULT_DB_PATH

_DATA_DIR = "/opt/linux-ai-server/data"

# Sabit-path DB'ler (her zaman data/ altında).
MEMORY_DB = f"{_DATA_DIR}/claude_memory.db"
COVERAGE_DB = f"{_DATA_DIR}/coverage.db"
RAG_METRICS_DB = f"{_DATA_DIR}/rag_metrics.db"

# server.db: prod systemd `DB_PATH` env'i set eder (events.py deseni); yoksa test-fallback.
# Tek runtime gerçeği — emit/read AYNI path'i kullanmalı yoksa sessizce tablosuz-path'e drop.


def server_db_path() -> str:
    """server.db kanonik yolu (env DB_PATH override'lı — events.py ile aynı semantik)."""
    return os.environ.get("DB_PATH") or DEFAULT_DB_PATH


def get_conn(
    db_path: str,
    *,
    readonly: bool = False,
    busy_timeout_ms: int = 5000,
    row_factory: bool = True,
    wal: bool = True,
) -> sqlite3.Connection:
    """Tek kanonik bağlantı. busy_timeout HER ZAMAN (lock-flap önler). readonly → uri mode=ro.

    busy_timeout WAL'den ÖNCE: kilitliyse hata yerine bekle (eksikliği gerçek olay üretti).
    WAL DB-düzeyinde kalıcı; readonly bağlantıda set edilmez (yazma gerektirir).
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) if readonly else sqlite3.connect(db_path)
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    if wal and not readonly:
        conn.execute("PRAGMA journal_mode=WAL")
    if row_factory:
        conn.row_factory = sqlite3.Row
    return conn
