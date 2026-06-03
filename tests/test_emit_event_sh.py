"""LIVESYS Faz 3.2 — bash emit-helper (scripts/emit-event.sh) interop testleri.

Bash uretici-noktalarinin yazdigini Python okuyucu (app.core.events) okuyabilmeli;
severity-normalize + eksik-alan-skip + SQL-escape bash tarafinda da tutarli olmali.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from app.core import events as ev

HELPER = Path(__file__).resolve().parent.parent / "scripts" / "emit-event.sh"


def _events_db(tmp_path):
    p = tmp_path / "server.db"
    con = sqlite3.connect(p)
    con.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "timestamp TEXT DEFAULT (datetime('now')), type TEXT, source TEXT, "
        "severity TEXT DEFAULT 'info', title TEXT, detail TEXT, payload TEXT, "
        "notified INTEGER DEFAULT 0)"
    )
    con.commit()
    con.close()
    return str(p)


def _emit(db, *args):
    return subprocess.run(
        [str(HELPER), *args],
        env={"DB_PATH": db, "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )


def test_bash_emit_is_readable_by_python(monkeypatch, tmp_path):
    db = _events_db(tmp_path)
    monkeypatch.setattr(ev, "DB_PATH", db)
    # warning -> warn (notifyable); bash uretici Python okuyucuyla tutarli olmali
    r = _emit(db, "job-outcome", "cron:demo-reset", "warning", "cron demo-reset partial", "rc=0 119/123")
    assert r.returncode == 0
    pend = ev.pending_notifications()
    assert len(pend) == 1
    assert pend[0]["severity"] == "warn"  # warning normalize edildi -> bildirilebilir
    assert pend[0]["source"] == "cron:demo-reset"


def test_bash_emit_critical_and_missing_field(tmp_path):
    db = _events_db(tmp_path)
    _emit(db, "job-outcome", "cron:test-runner", "critical", "cron test-runner fail", "rc=1 boom")
    _emit(db, "x", "", "info", "eksik-source")  # zorunlu alan eksik -> no-op
    con = sqlite3.connect(db)
    rows = con.execute("SELECT severity, source, notified FROM events ORDER BY id").fetchall()
    con.close()
    assert rows == [("critical", "cron:test-runner", 0)]  # tek satir; eksik-alanli yazilmadi


def test_bash_emit_sql_escape(tmp_path):
    db = _events_db(tmp_path)
    # tek-tirnak iceren detail SQL-injection/parse-bozmadan yazilmali
    _emit(db, "job-outcome", "cron:x", "warn", "title", "it's a 'tricky' detail")
    con = sqlite3.connect(db)
    detail = con.execute("SELECT detail FROM events").fetchone()[0]
    con.close()
    assert detail == "it's a 'tricky' detail"


def test_bash_emit_missing_db_is_safe(tmp_path):
    # DB yoksa fail-safe exit 0 (caginan cron-job'u dusurmemeli)
    r = _emit(str(tmp_path / "none.db"), "job-outcome", "cron:x", "warn", "t", "d")
    assert r.returncode == 0
