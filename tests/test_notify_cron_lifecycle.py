"""notify-cron auto-bug yaşam-döngüsü — Slice A (auto-resolve) + Slice C (tekrar-deseni).

A: kaynak RESOLVE_QUIET_MIN dk sessizse (son critical eski) AUTO-alert bug auto-resolve.
C: kaynağın son RECUR_DAYS'te >=RECUR_THRESHOLD critical'i varsa alert'e 🔁 TEKRARLAYAN.
curl PATH'te gölgelenir.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "automation" / "notify-cron.sh"


def _mk_srv(path: Path) -> None:
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "timestamp TEXT DEFAULT (datetime('now')), type TEXT, source TEXT, "
        "severity TEXT, title TEXT, detail TEXT, notified INTEGER DEFAULT 0, acked INTEGER DEFAULT 0)"
    )
    con.commit()
    con.close()


def _mk_mem(path: Path) -> None:
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE discoveries (id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, "
        "type TEXT, title TEXT, details TEXT, resolved INTEGER DEFAULT 0, "
        "created_at TEXT DEFAULT (datetime('now')), status TEXT DEFAULT 'active', rationale TEXT)"
    )
    con.commit()
    con.close()


def _event(srv: Path, source: str, when: str, notified: int = 0) -> None:
    con = sqlite3.connect(str(srv))
    con.execute(
        f"INSERT INTO events (timestamp, type, source, severity, title, notified) "
        f"VALUES (datetime('now','{when}'), 'alert', ?, 'critical', 't', ?)",
        (source, notified),
    )
    con.commit()
    con.close()


def _discovery(mem: Path, title: str) -> None:
    con = sqlite3.connect(str(mem))
    con.execute(
        "INSERT INTO discoveries (project, type, title, status) VALUES ('linux-ai-server','bug',?,'active')",
        (title,),
    )
    con.commit()
    con.close()


def _status(mem: Path, title: str) -> str:
    con = sqlite3.connect(str(mem))
    row = con.execute("SELECT status FROM discoveries WHERE title=?", (title,)).fetchone()
    con.close()
    return row[0] if row else ""


def _fake_curl(bindir: Path, capture: Path) -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    fake = bindir / "curl"
    fake.write_text(
        f'#!/bin/bash\nprintf "%s\\n" "$*" >> {str(capture)!r}\nif printf "%s" "$*" | grep -q "http_code"; then printf "200"; fi\n'
    )
    fake.chmod(0o755)


def _run(tmp_path: Path) -> str:
    srv, mem = tmp_path / "srv.db", tmp_path / "mem.db"
    capture = tmp_path / "curl.log"
    _fake_curl(tmp_path / "bin", capture)
    subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
            "NOTIFY_CRON_ENABLED": "true",
            "NOTIFY_ENV_FILE": "/dev/null",
            "DB_PATH": str(srv),
            "MEMORY_DB": str(mem),
            "API_BASE": "http://localhost:8420",
            "TELEGRAM_BOT_TOKEN": "x",
            "TELEGRAM_CHAT_ID": "1",
            "MEMORY_API_KEY": "mk",
            "NOTIFY_CRON_LOG": str(tmp_path / "n.log"),
        },
        capture_output=True,
        text=True,
        timeout=25,
    )
    return capture.read_text() if capture.exists() else ""


# ── Slice A: auto-resolve ────────────────────────────────────────


def test_auto_resolve_recovered_and_keeps_broken(tmp_path):
    srv, mem = tmp_path / "srv.db", tmp_path / "mem.db"
    _mk_srv(srv)
    _mk_mem(mem)
    _event(srv, "cron:eski", "-120 minutes")  # düzelmiş (sessiz)
    _discovery(mem, "AUTO-alert: cron:eski")
    _event(srv, "cron:taze", "-5 minutes")  # hâlâ bozuk
    _discovery(mem, "AUTO-alert: cron:taze")

    _run(tmp_path)

    assert _status(mem, "AUTO-alert: cron:eski") == "completed"  # düzeldi -> resolve
    assert _status(mem, "AUTO-alert: cron:taze") == "active"  # hâlâ bozuk -> dokunma


def test_auto_resolve_only_touches_auto_alert(tmp_path):
    """Manuel (AUTO-alert olmayan) bug'lara DOKUNMAZ."""
    srv, mem = tmp_path / "srv.db", tmp_path / "mem.db"
    _mk_srv(srv)
    _mk_mem(mem)
    _discovery(mem, "Manuel bug elle açıldı")

    _run(tmp_path)

    assert _status(mem, "Manuel bug elle açıldı") == "active"


# ── Slice C: tekrar-deseni ───────────────────────────────────────


def test_recurring_critical_flags_message(tmp_path):
    """Kaynağın >=3 critical'i (7g) varsa alert mesajında 🔁 TEKRARLAYAN."""
    srv, mem = tmp_path / "srv.db", tmp_path / "mem.db"
    _mk_srv(srv)
    _mk_mem(mem)
    _event(srv, "cron:tekrar", "-2 days", notified=1)
    _event(srv, "cron:tekrar", "-1 days", notified=1)
    _event(srv, "cron:tekrar", "-1 minutes", notified=0)  # pending -> işlenir

    cap = _run(tmp_path)

    assert "TEKRARLAYAN" in cap
    assert "3x" in cap


def test_single_critical_no_recurring_flag(tmp_path):
    """Tek critical -> tekrar-uyarısı YOK."""
    srv, mem = tmp_path / "srv.db", tmp_path / "mem.db"
    _mk_srv(srv)
    _mk_mem(mem)
    _event(srv, "cron:tek", "-1 minutes", notified=0)

    cap = _run(tmp_path)

    assert "TEKRARLAYAN" not in cap
