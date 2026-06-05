"""scripts/db-query.sh — data-analist engine-zorlamalı read-only SQL helper.

sqlite3 -readonly: yazma MOTORDA reddedilir (pattern değil). alias-guard: yalnız
server/coverage. Test temp-DB enjekte eder (DB_QUERY_SERVER/COVERAGE), prod /opt default.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "db-query.sh"


def _mk_db(path: Path) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, source TEXT, severity TEXT)")
    con.execute("INSERT INTO events (source, severity) VALUES ('cron:x','critical')")
    con.execute("INSERT INTO events (source, severity) VALUES ('memory','warn')")
    con.commit()
    con.close()


def _run(tmp_path: Path, alias: str, sql: str):
    db = tmp_path / "server.db"
    if not db.exists():
        _mk_db(db)
    return subprocess.run(
        ["bash", str(SCRIPT), alias, sql],
        env={
            "PATH": "/usr/bin:/bin",
            "DB_QUERY_SERVER": str(db),
            "DB_QUERY_COVERAGE": str(tmp_path / "coverage.db"),
        },
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_select_returns_data(tmp_path):
    r = _run(tmp_path, "server", "SELECT COUNT(*) AS n FROM events;")
    assert r.returncode == 0
    assert "2" in r.stdout


def test_write_rejected_by_engine(tmp_path):
    """DELETE -> motor reddeder ('readonly database'); satır SİLİNMEZ."""
    r = _run(tmp_path, "server", "DELETE FROM events;")
    assert "readonly database" in (r.stdout + r.stderr).lower()
    # gerçekten silinmedi mi
    db = tmp_path / "server.db"
    con = sqlite3.connect(str(db))
    n = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    con.close()
    assert n == 2  # DELETE etkisiz


def test_invalid_alias_rejected(tmp_path):
    r = _run(tmp_path, "/etc/passwd", "SELECT 1;")
    assert r.returncode == 2
    assert "geçersiz db alias" in (r.stdout + r.stderr)


def test_empty_sql_rejected(tmp_path):
    r = _run(tmp_path, "server", "")
    assert r.returncode == 2


def test_dot_command_shell_blocked(tmp_path):
    """Codex P1: .shell dot-command -readonly'yi aşıp RCE yapardı -> REDDEDİLİR."""
    r = _run(tmp_path, "server", ".shell echo PWNED")
    assert r.returncode == 2
    assert "PWNED" not in r.stdout
    assert "dot-command" in (r.stdout + r.stderr)


def test_dot_command_output_file_blocked(tmp_path):
    """.output (dosya-yazma) -> reddedilir, dosya oluşmaz."""
    target = tmp_path / "leak.txt"
    r = _run(tmp_path, "server", f".output {target}\nSELECT 1;")
    assert r.returncode == 2
    assert not target.exists()


def test_drop_table_rejected(tmp_path):
    """DROP da motor-reddi (yazma)."""
    r = _run(tmp_path, "server", "DROP TABLE events;")
    assert "readonly" in (r.stdout + r.stderr).lower()
    db = tmp_path / "server.db"
    con = sqlite3.connect(str(db))
    exists = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='events'").fetchone()[0]
    con.close()
    assert exists == 1  # tablo duruyor
