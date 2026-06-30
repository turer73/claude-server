"""notify-cron critical-cooldown (#100224 TIER-2 reliability).

Davranış sözleşmesi:
  - İLK critical HER ZAMAN Telegram'a gider (warn→critical escalation kaçmaz).
  - Aynı kaynağın YAKIN-ZAMANDA zaten-bildirilmiş (notified=1) critical'i varsa
    SONRAKİ critical collapse olur (notified=1 işaretlenir, Telegram'a GİTMEZ).
    Hedef: restart-storm / flapping / outage-burst flood'unu kes.
  - escalation:/remediation: önekleri MUAF (devops zaten 30dk throttle'lı).
  - warn-geçmişi critical-cooldown'ı tetiklemez (yalnız severity=critical sayılır).

curl PATH'te gölgelenir; Telegram çağrısı 'sendMessage' içeren args ile ayırt edilir.
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


def _event(srv: Path, source: str, when: str, sev: str = "critical", notified: int = 0) -> int:
    con = sqlite3.connect(str(srv))
    cur = con.execute(
        f"INSERT INTO events (timestamp, type, source, severity, title, notified) VALUES (datetime('now','{when}'), 'alert', ?, ?, 't', ?)",
        (source, sev, notified),
    )
    con.commit()
    rid = cur.lastrowid
    con.close()
    return rid


def _notified(srv: Path, rid: int) -> int:
    con = sqlite3.connect(str(srv))
    row = con.execute("SELECT notified FROM events WHERE id=?", (rid,)).fetchone()
    con.close()
    return row[0] if row else -1


def _fake_curl(bindir: Path, capture: Path) -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    fake = bindir / "curl"
    fake.write_text(
        f'#!/bin/bash\nprintf "%s\\n" "$*" >> {str(capture)!r}\nif printf "%s" "$*" | grep -q "http_code"; then printf "200"; fi\n'
    )
    fake.chmod(0o755)


def _run(tmp_path: Path, extra_env: dict | None = None) -> str:
    srv = tmp_path / "srv.db"
    capture = tmp_path / "curl.log"
    _fake_curl(tmp_path / "bin", capture)
    env = {
        "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
        "NOTIFY_CRON_ENABLED": "true",
        "NOTIFY_ENV_FILE": "/dev/null",
        "DB_PATH": str(srv),
        "MEMORY_DB": str(tmp_path / "mem.db"),  # yok -> reconcile no-op
        "API_BASE": "http://localhost:8420",
        "TELEGRAM_BOT_TOKEN": "x",
        "TELEGRAM_CHAT_ID": "1",
        # MEMORY_API_KEY YOK -> save_discovery atlanır (Telegram curl'ünü izole et)
        "NOTIFY_CRON_LOG": str(tmp_path / "n.log"),
    }
    if extra_env:
        env.update(extra_env)
    subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=25)
    return capture.read_text() if capture.exists() else ""


def test_first_critical_always_sends(tmp_path):
    """Önceden bildirilmiş critical YOK -> ilk critical Telegram'a gider."""
    srv = tmp_path / "srv.db"
    _mk_srv(srv)
    rid = _event(srv, "disk", "-10 seconds", notified=0)

    cap = _run(tmp_path)

    assert "sendMessage" in cap  # Telegram denendi
    assert _notified(srv, rid) == 1


def test_repeat_critical_collapsed_within_window(tmp_path):
    """Aynı kaynağın 2dk önce bildirilmiş critical'i var -> yeni critical collapse
    (notified=1 ama Telegram'a GİTMEZ). Restart-storm / flapping flood kesici."""
    srv = tmp_path / "srv.db"
    _mk_srv(srv)
    _event(srv, "disk", "-2 minutes", notified=1)  # zaten bildirildi
    rid = _event(srv, "disk", "-5 seconds", notified=0)  # pending tekrar

    cap = _run(tmp_path, {"CRITICAL_COOLDOWN_SECONDS": "900"})

    assert "sendMessage" not in cap  # Telegram'a GİTMEDİ (collapse)
    assert _notified(srv, rid) == 1  # ama event handled (retry yok, no-loss)


def test_repeat_critical_sends_after_window(tmp_path):
    """Önceki bildirim pencere-DIŞI (eski) -> hatırlatma olarak yeniden gönderilir."""
    srv = tmp_path / "srv.db"
    _mk_srv(srv)
    _event(srv, "disk", "-30 minutes", notified=1)  # eski (>15dk)
    rid = _event(srv, "disk", "-5 seconds", notified=0)

    cap = _run(tmp_path, {"CRITICAL_COOLDOWN_SECONDS": "900"})

    assert "sendMessage" in cap
    assert _notified(srv, rid) == 1


def test_warn_history_does_not_trigger_critical_cooldown(tmp_path):
    """Yalnız warn-geçmişi varsa -> ilk critical YİNE geçer (escalation kaçmaz)."""
    srv = tmp_path / "srv.db"
    _mk_srv(srv)
    _event(srv, "disk", "-1 minutes", sev="warn", notified=1)  # warn bildirildi
    rid = _event(srv, "disk", "-5 seconds", sev="critical", notified=0)  # ilk critical

    cap = _run(tmp_path, {"CRITICAL_COOLDOWN_SECONDS": "900"})

    assert "sendMessage" in cap  # warn-cooldown critical'i susturmaz
    assert _notified(srv, rid) == 1


def test_escalation_source_exempt_from_critical_cooldown(tmp_path):
    """escalation: önekli kaynak MUAF -> yakın bildirim olsa bile yine gönderilir
    (devops _escalate_persistent zaten 30dk throttle'lı)."""
    srv = tmp_path / "srv.db"
    _mk_srv(srv)
    _event(srv, "escalation:disk", "-2 minutes", notified=1)
    rid = _event(srv, "escalation:disk", "-5 seconds", notified=0)

    cap = _run(tmp_path, {"CRITICAL_COOLDOWN_SECONDS": "900"})

    assert "sendMessage" in cap  # muaf -> gönderildi
    assert _notified(srv, rid) == 1


def test_critical_cooldown_disabled_when_zero(tmp_path):
    """CRITICAL_COOLDOWN_SECONDS=0 -> collapse kapalı, her critical gider."""
    srv = tmp_path / "srv.db"
    _mk_srv(srv)
    _event(srv, "disk", "-2 minutes", notified=1)
    rid = _event(srv, "disk", "-5 seconds", notified=0)

    cap = _run(tmp_path, {"CRITICAL_COOLDOWN_SECONDS": "0"})

    assert "sendMessage" in cap
    assert _notified(srv, rid) == 1
