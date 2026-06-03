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
