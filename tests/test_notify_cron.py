"""LIVESYS FAZ 3.2 (d) — notify-cron.sh DISABLED-safety guard.

KRİTİK invariant: NOTIFY_CRON_ENABLED set değil/false iken notify-cron HİÇBİR ŞEY
yapmadan exit 0 (n8n POST yok, mark_notified yok). Enable = surer cross-verify +
kullanıcı go. Bu test o güvenlik kapısını regresyona karşı kilitler.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "automation" / "notify-cron.sh"


# NOTIFY_ENV_FILE=/dev/null: _envget gerçek /opt/.env'i OKUMAZ (boş) -> testler canlı
# .env'den İZOLE (bu-box NOTIFY_CRON_ENABLED=true ise bile deterministik default-disabled).
_ISO = {"NOTIFY_ENV_FILE": "/dev/null", "PATH": "/usr/bin:/bin"}


def test_notify_cron_disabled_is_noop():
    # NOTIFY_CRON_ENABLED=false -> flag-gate'te hemen exit 0 (DB/n8n'e dokunmadan)
    r = subprocess.run(
        ["bash", str(SCRIPT)],
        env={**_ISO, "NOTIFY_CRON_ENABLED": "false"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0
    assert r.stdout == ""  # hiçbir çıktı/işlem yok


def test_notify_cron_unset_defaults_disabled():
    # Flag hiç set değilse + .env-izole (/dev/null) -> default false -> no-op (fail-safe)
    r = subprocess.run(
        ["bash", str(SCRIPT)],
        env=_ISO,  # NOTIFY_CRON_ENABLED yok + NOTIFY_ENV_FILE=/dev/null (gerçek .env izole)
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0
    assert r.stdout == ""


def test_notify_cron_enabled_no_pending_outputs_outcome(tmp_path):
    # ENABLED=true ama event yok → OUTCOME:pass|no-pending (cron-wrap monitoring icin)
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("")
    empty_db = tmp_path / "empty.db"
    import sqlite3

    conn = sqlite3.connect(str(empty_db))
    conn.execute(
        "CREATE TABLE events ("
        "id INTEGER PRIMARY KEY, type TEXT, source TEXT, "
        "severity TEXT, title TEXT, detail TEXT, "
        "notified INTEGER DEFAULT 0, timestamp TEXT)"
    )
    conn.commit()
    conn.close()
    r = subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            "NOTIFY_CRON_ENABLED": "true",
            "TELEGRAM_BOT_TOKEN": "test-dummy-token",
            "TELEGRAM_CHAT_ID": "99999",
            "NOTIFY_ENV_FILE": str(empty_env),
            "DB_PATH": str(empty_db),
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0
    assert "OUTCOME: pass" in r.stdout
    assert "no-pending" in r.stdout


def test_notify_cron_suggest_action_actionable():
    """Aksiyon-önerisi: suggest_action() haber-vermekle kalmaz, ne-yapmalı + tanı üretir.
    Fonksiyonu izole çıkar (sed) + eval + çeşitli source'larda doğrula."""
    extract = "f=$(sed -n '/^suggest_action()/,/^}/p' automation/notify-cron.sh); eval \"$f\"; "
    r = subprocess.run(
        ["bash", "-c", extract + "suggest_action memory; suggest_action 'service:linux-ai-server'; suggest_action 'escalation:disk'"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=10,
    )
    out = r.stdout
    assert "Öneri" in out  # ne-yapmalı
    assert "systemctl restart linux-ai-server" in out  # somut komut (service)
    assert "MANUEL" in out  # escalation -> manuel müdahale
