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


def test_notify_cron_disabled_is_noop():
    # NOTIFY_CRON_ENABLED=false -> flag-gate'te hemen exit 0 (DB/n8n'e dokunmadan)
    r = subprocess.run(
        ["bash", str(SCRIPT)],
        env={"NOTIFY_CRON_ENABLED": "false", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0
    assert r.stdout == ""  # hiçbir çıktı/işlem yok


def test_notify_cron_unset_defaults_disabled():
    # Flag hiç set değilse de default false -> no-op (fail-safe varsayılan)
    # NOTIFY_ENV_FILE=/dev/null: prod .env (NOTIFY_CRON_ENABLED=true olabilir) izole et
    r = subprocess.run(
        ["bash", str(SCRIPT)],
        env={"PATH": "/usr/bin:/bin", "NOTIFY_ENV_FILE": "/dev/null"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0
    assert r.stdout == ""
