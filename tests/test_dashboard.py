"""#1244 repro — dashboard unread_notes sayacı held/rejected notları SAYMAMALI.

G4 completeness-guard ilk-koşum keşfi (disc#1244): sayaç legacy `read=0` idi — held-not
sayaca sızıyordu (policy-gate #1222 teslim-semantiğine aykırı; digest memory_delta'da aynı
konu önceden düzeltilmişti). Fix rescue/1244 (132c3a1, otonom-spawn) + bu testler.

G1: fix geri alınırsa (COALESCE-filtre düşerse) test_dashboard_unread_excludes_held FAIL.
Dürüst-sınır: dashboard hâlâ GLOBAL read=0 (per-device DEĞİL) — genel-bakış ekranında
"kimin perspektifi" tasarım-sorusu, kapsam-dışı (#100389).
"""

from __future__ import annotations

import sqlite3

import app.api.memory as mem
from app.api.memory.dashboard import _dashboard_query


def _make_full_db(tmp_path):
    """_dashboard_query'nin dokunduğu TÜM tablolar (gerçek-yol: fonksiyon komple çağrılır)."""
    db = tmp_path / "claude_memory.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE memories (id INTEGER PRIMARY KEY, active INT);
        CREATE TABLE sessions (id INTEGER PRIMARY KEY, session_num INT, date TEXT, device_name TEXT, platform TEXT, summary TEXT);
        CREATE TABLE tasks_log (id INTEGER PRIMARY KEY);
        CREATE TABLE discoveries (id INTEGER PRIMARY KEY, project TEXT, type TEXT, title TEXT, status TEXT,
                                  device_name TEXT, created_at TEXT, read_count INT DEFAULT 0);
        CREATE TABLE devices (name TEXT, platform TEXT, hostname TEXT, tailscale_ip TEXT, last_seen TEXT);
        CREATE TABLE notes (id INTEGER PRIMARY KEY, read INT DEFAULT 0, read_by TEXT DEFAULT '', status TEXT DEFAULT 'active');
        INSERT INTO notes (read, status) VALUES (0, 'active');   -- sayılmalı
        INSERT INTO notes (read, status) VALUES (0, 'held');     -- SAYILMAMALI (#1222 teslim-semantiği)
        INSERT INTO notes (read, status) VALUES (0, 'rejected'); -- SAYILMAMALI
        INSERT INTO notes (read, status) VALUES (0, NULL);       -- legacy NULL = active, sayılmalı
        INSERT INTO notes (read, status) VALUES (1, 'active');   -- okunmuş, sayılmamalı
        """
    )
    con.commit()
    con.close()
    return db


def test_dashboard_unread_excludes_held(monkeypatch, tmp_path):
    db = _make_full_db(tmp_path)
    monkeypatch.setattr(mem, "DB_PATH", str(db))
    result = _dashboard_query()
    # active(1) + legacy-NULL(1) = 2; held + rejected + okunmuş DIŞARIDA
    assert result["stats"]["unread_notes"] == 2
