"""KONSOLIDASYON (2026-06-04) — alert-check + health-check artık events-spine'a yazar.

Eski: alert-check->n8n self-healing + direkt-Telegram; health-check->direkt-Telegram.
Yeni: ikisi de emit-event.sh ile server.db.events'e -> notify-cron -> Telegram (buton'lu).
Bu testler: (1) down/unhealthy geçişinde spine'a critical event düşer, (2) edge-detection
(aynı durum tekrarında ikinci event YOK), (3) up/healthy iken event yok.

curl PATH'te sahte-binary ile gölgelenir (gerçek API'ye gitmez); emit-event.sh gerçek
çalışır, test DB'ye yazar.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALERT_CHECK = ROOT / "automation" / "alert-check.sh"
HEALTH_CHECK = ROOT / "automation" / "health-check.sh"
EMIT = ROOT / "scripts" / "emit-event.sh"


def _mk_events_db(path: Path) -> None:
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "timestamp TEXT NOT NULL DEFAULT (datetime('now')), "
        "type TEXT NOT NULL, source TEXT NOT NULL, "
        "severity TEXT NOT NULL DEFAULT 'info', title TEXT NOT NULL, "
        "detail TEXT, notified INTEGER NOT NULL DEFAULT 0)"
    )
    con.commit()
    con.close()


def _fake_curl(bindir: Path, http_code: str, body: str) -> None:
    """PATH'e sahte curl koy: gövde + '\\n' + http_code yazar (alert-check -w formatı),
    POST gövdesi için de aynı body'i döner (health-check JSON parse eder)."""
    bindir.mkdir(parents=True, exist_ok=True)
    fake = bindir / "curl"
    fake.write_text(
        "#!/bin/bash\n"
        # -w '\n%{http_code}' kullanılırsa http_code ekle; değilse sadece body.
        f'if printf "%s" "$*" | grep -q -- "%{{http_code}}"; then\n'
        f'  printf "%s\\n%s" {body!r} {http_code!r}\n'
        f"else\n"
        f'  printf "%s" {body!r}\n'
        f"fi\n"
    )
    fake.chmod(0o755)


def _env(tmp_path: Path, db: Path, state: Path):
    return {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "EMIT_EVENT": str(EMIT),
        "DB_PATH": str(db),
        "NOTIFY_ENV_FILE": "/dev/null",
        "ALERT_CHECK_STATE": str(state),
        "HEALTH_CHECK_STATE": str(state),
        "ALERT_CHECK_LOG": str(tmp_path / "a.log"),
        "HEALTH_CHECK_LOG": str(tmp_path / "h.log"),
        "INTERNAL_API_KEY": "test-key",
    }


def _events(db: Path) -> list[tuple]:
    con = sqlite3.connect(str(db))
    rows = con.execute("SELECT source, severity, title FROM events ORDER BY id").fetchall()
    con.close()
    return rows


# ── alert-check (monitoring pipeline watchdog) ──────────────────


def test_alert_check_metrics_down_emits_to_spine(tmp_path):
    db = tmp_path / "ev.db"
    _mk_events_db(db)
    state = tmp_path / "state"
    _fake_curl(tmp_path / "bin", "503", "")
    r = subprocess.run(["bash", str(ALERT_CHECK)], env=_env(tmp_path, db, state), capture_output=True, text=True, timeout=15)
    assert r.returncode == 0
    rows = _events(db)
    assert len(rows) == 1
    assert rows[0][0] == "monitoring"
    assert rows[0][1] == "critical"


def test_alert_check_edge_no_double_emit(tmp_path):
    """Aynı 'down' durumu ikinci run'da TEKRAR event basmaz (edge-detection)."""
    db = tmp_path / "ev.db"
    _mk_events_db(db)
    state = tmp_path / "state"
    _fake_curl(tmp_path / "bin", "503", "")
    env = _env(tmp_path, db, state)
    subprocess.run(["bash", str(ALERT_CHECK)], env=env, capture_output=True, text=True, timeout=15)
    subprocess.run(["bash", str(ALERT_CHECK)], env=env, capture_output=True, text=True, timeout=15)
    assert len(_events(db)) == 1  # iki run, tek event


def test_alert_check_metrics_up_no_event(tmp_path):
    db = tmp_path / "ev.db"
    _mk_events_db(db)
    state = tmp_path / "state"
    _fake_curl(tmp_path / "bin", "200", '{"cpu_percent":10}')
    r = subprocess.run(["bash", str(ALERT_CHECK)], env=_env(tmp_path, db, state), capture_output=True, text=True, timeout=15)
    assert r.returncode == 0
    assert _events(db) == []


# ── health-check (app watchdog) ─────────────────────────────────


def test_health_check_unhealthy_emits_service_event(tmp_path):
    db = tmp_path / "ev.db"
    _mk_events_db(db)
    state = tmp_path / "state"
    _fake_curl(tmp_path / "bin", "200", '{"healthy": false}')
    r = subprocess.run(["bash", str(HEALTH_CHECK)], env=_env(tmp_path, db, state), capture_output=True, text=True, timeout=15)
    assert r.returncode == 0
    rows = _events(db)
    assert len(rows) == 1
    assert rows[0][0] == "service:linux-ai-server"  # -> [🔧 Uygula] restart butonu
    assert rows[0][1] == "critical"


def test_health_check_healthy_no_event(tmp_path):
    db = tmp_path / "ev.db"
    _mk_events_db(db)
    state = tmp_path / "state"
    _fake_curl(tmp_path / "bin", "200", '{"healthy": true}')
    r = subprocess.run(["bash", str(HEALTH_CHECK)], env=_env(tmp_path, db, state), capture_output=True, text=True, timeout=15)
    assert r.returncode == 0
    assert _events(db) == []


def test_health_check_edge_no_double_emit(tmp_path):
    db = tmp_path / "ev.db"
    _mk_events_db(db)
    state = tmp_path / "state"
    _fake_curl(tmp_path / "bin", "200", '{"healthy": false}')
    env = _env(tmp_path, db, state)
    subprocess.run(["bash", str(HEALTH_CHECK)], env=env, capture_output=True, text=True, timeout=15)
    subprocess.run(["bash", str(HEALTH_CHECK)], env=env, capture_output=True, text=True, timeout=15)
    assert len(_events(db)) == 1
