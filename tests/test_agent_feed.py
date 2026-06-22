"""agent-feed.sh — type=exception paneli (gap-2 feed-wire, follow-up 3).

Bash script'i subprocess ile çalıştırıp sentetik exception-event'in session-start
feed'inde göründüğünü doğrular (reference_severity_warn_pages: eskiden type=exception
feed'de görünmüyordu → bu panel o boşluğu kapatır)."""

import os
import sqlite3
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "agent-feed.sh"


def _run(srv, mem):
    return subprocess.run(
        ["bash", str(SCRIPT), "--hours", "24"],
        env={"AGENT_FEED_SRV_DB": str(srv), "AGENT_FEED_MEM_DB": str(mem), "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout


def test_agent_feed_surfaces_exceptions(tmp_path):
    srv = tmp_path / "server.db"
    con = sqlite3.connect(srv)
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, timestamp TEXT, type TEXT, severity TEXT, source TEXT, title TEXT)")
    # Aynı fingerprint 2× → tek satır + (2x) sayım (GROUP BY source)
    for _ in range(2):
        con.execute(
            "INSERT INTO events (timestamp,type,severity,source,title) VALUES "
            "(datetime('now','-1 hours'),'exception','warn','exception:ValueError:app/api/x.py:foo','ValueError @ app/api/x.py:foo')"
        )
    con.commit()
    con.close()
    mem = tmp_path / "mem.db"
    sqlite3.connect(mem).close()  # boş → notes yok, fail-safe atlar

    out = _run(srv, mem)
    assert "🐛 Unhandled exception" in out
    assert "ValueError @ app/api/x.py:foo (2x)" in out


def test_agent_feed_no_exception_panel_when_none(tmp_path):
    """Exception yoksa panel görünmez (gürültü-yok)."""
    srv = tmp_path / "server.db"
    con = sqlite3.connect(srv)
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, timestamp TEXT, type TEXT, severity TEXT, source TEXT, title TEXT)")
    con.commit()
    con.close()
    mem = tmp_path / "mem.db"
    sqlite3.connect(mem).close()

    out = _run(srv, mem)
    assert "🐛 Unhandled exception" not in out
