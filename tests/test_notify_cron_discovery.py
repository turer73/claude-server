"""notify-cron: critical event -> otomatik hata-hafızası (discovery=bug).

"Sadece hata varsa" = yalnız critical. Stabil başlık (AUTO-alert: <source>) -> server
dedup tekrar-eden hatayı tek kayıtta tutar. curl PATH'te gölgelenir (Telegram + memory
POST yakalanır, gerçek ağ yok).
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "automation" / "notify-cron.sh"


def _mk_db(path: Path, severity: str) -> None:
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "timestamp TEXT DEFAULT (datetime('now')), type TEXT, source TEXT, "
        "severity TEXT, title TEXT, detail TEXT, notified INTEGER DEFAULT 0, acked INTEGER DEFAULT 0)"
    )
    con.execute(
        "INSERT INTO events (type, source, severity, title, detail) VALUES (?,?,?,?,?)",
        ("alert", "cron:renderhane-balance", severity, "cron fail", "rc=1"),
    )
    con.commit()
    con.close()


def _fake_curl(bindir: Path, capture: Path) -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    fake = bindir / "curl"
    fake.write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$*" >> {str(capture)!r}\n'
        # Telegram çağrısı -w %{http_code} kullanır -> 200 döndür ki SENT sayılsın.
        'if printf "%s" "$*" | grep -q "http_code"; then printf "200"; fi\n'
    )
    fake.chmod(0o755)


def _run(tmp_path: Path, severity: str) -> str:
    db = tmp_path / "ev.db"
    _mk_db(db, severity)
    capture = tmp_path / "curl.log"
    _fake_curl(tmp_path / "bin", capture)
    subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
            "NOTIFY_CRON_ENABLED": "true",
            "NOTIFY_ENV_FILE": "/dev/null",
            "DB_PATH": str(db),
            "API_BASE": "http://localhost:8420",
            "TELEGRAM_BOT_TOKEN": "x",
            "TELEGRAM_CHAT_ID": "1",
            "MEMORY_API_KEY": "mk-test",
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    return capture.read_text() if capture.exists() else ""


def test_critical_event_saves_discovery(tmp_path):
    cap = _run(tmp_path, "critical")
    assert "/api/v1/memory/discoveries" in cap
    assert "AUTO-alert: cron:renderhane-balance" in cap
    assert "bug" in cap  # type=bug


def test_warn_event_does_not_save_discovery(tmp_path):
    """'Sadece hata varsa' -> warn için discovery YAZILMAZ (yalnız Telegram)."""
    cap = _run(tmp_path, "warn")
    assert "/api/v1/memory/discoveries" not in cap
