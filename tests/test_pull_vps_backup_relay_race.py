"""pull-vps-backup relay — "hala kosuyor" ile "bitti ama sonuc yok" ayri raporlanmali.

2026-09-05 03:00 UTC: relay `cron:vps-backup-push` icin CRITICAL bastirdi
("stale-relay: OUTCOME ts=2026-09-04 ... SIGKILL/stale-log?") ve meta-monitor
bekciyi OLU isaretledi. Yedek SAGLAMDI: VPS backup.sh 03:00'te basliyor, o gun
111 dakika surdu (15dk -> 111dk trendi) ve klipper 04:20'de sonucu okurken
uretici HENUZ BITMEMISTI. Yani ariza degil URETICI-TUKETICI YARISI.

Alarmin sinifi yanlis oldugu icin "yedek alinamadi mi?" diye bakildi; kanit
ancak VPS log'u elle okununca cikti. Ders (PR#377 ile ayni sinif): iki farkli
ariza tek mesaja gomulmesin.

Testler `ssh`'i stub'lar; ag/VPS gerekmez ve CANLI server.db'ye yazilmaz.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "automation" / "pull-vps-backup.sh"

_YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


def _stub_ssh(bin_dir: Path, *, backup_running: bool) -> None:
    """cron.log TAZE + son OUTCOME satiri DUNE ait; backup.sh kosuyor/kosmuyor."""
    ssh = bin_dir / "ssh"
    ssh.write_text(
        "#!/bin/bash\n"
        'args="$*"\n'
        'case "$args" in\n'
        f'  *"stat -c %Y"*) echo {int(time.time())} ;;\n'
        f"  *OUTCOME*) echo 'OUTCOME: partial | local OK offsite-eksik r2=1 gdrive=0 | ts:{_YESTERDAY}' ;;\n"
        f"  *pgrep*) exit {0 if backup_running else 1} ;;\n"
        "esac\n"
        "exit 0\n"
    )
    ssh.chmod(0o755)


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE cron_outcomes (id INTEGER PRIMARY KEY, timestamp TEXT DEFAULT (datetime('now')), "
            "job TEXT, result TEXT, rc INTEGER, source TEXT, detail TEXT, attempt_no INTEGER DEFAULT 1)"
        )
        conn.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, timestamp TEXT DEFAULT (datetime('now')), "
            "type TEXT, source TEXT, severity TEXT, title TEXT, detail TEXT, notified INTEGER DEFAULT 0)"
        )
        conn.commit()
    finally:
        conn.close()


def _run(tmp_path: Path, bin_dir: Path, db: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "VPS_HOST": "root@test-invalid",
            "VPS_BACKUP_TARGET": str(tmp_path / "out"),
            "VPS_BACKUP_MOUNT": "/",
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
            "KUMA_BACKUP_PUSH_URL": "",
            "DB_PATH": str(db),
        },
    )


def _relay_row(db: Path) -> tuple[str, str]:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT result, COALESCE(detail,'') FROM cron_outcomes WHERE job='vps-backup-push' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row, "relay cron_outcomes satiri yazmadi"
    return row[0], row[1]


def _backup_events(db: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(db)
    try:
        return list(conn.execute("SELECT severity, COALESCE(detail,'') FROM events WHERE source='vps:backup-push'"))
    finally:
        conn.close()


@pytest.mark.skipif(shutil.which("mountpoint") is None, reason="mountpoint yok")
def test_producer_still_running_is_not_reported_as_failure(tmp_path: Path) -> None:
    """REPRO: VPS backup.sh hala kosuyorken relay CRITICAL "stale-relay" basmamali.

    Duzeltme oncesi: bugunku ts yok -> kosulsuz fail + critical (yedek saglamken
    bekci OLU isaretlendi). Sonrasi: partial + "HALA KOSUYOR" (warn).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub_ssh(bin_dir, backup_running=True)
    db = tmp_path / "server.db"
    _make_db(db)

    _run(tmp_path, bin_dir, db)

    result, detail = _relay_row(db)
    assert result == "partial", f"result={result!r} detail={detail!r}"
    assert "HALA KOSUYOR" in detail, detail
    assert "SIGKILL" not in detail, detail

    sevs = [s for s, _ in _backup_events(db)]
    assert "critical" not in sevs, f"yaris durumu critical'a cikti: {_backup_events(db)}"


@pytest.mark.skipif(shutil.which("mountpoint") is None, reason="mountpoint yok")
def test_producer_finished_without_todays_outcome_is_still_failure(tmp_path: Path) -> None:
    """Karsi-taraf: backup.sh KOSMUYOR ve bugunku sonuc yoksa bu GERCEK ariza — fail kalmali.

    Duzeltmenin alarmi topyekun susturmadiginin kaniti.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub_ssh(bin_dir, backup_running=False)
    db = tmp_path / "server.db"
    _make_db(db)

    _run(tmp_path, bin_dir, db)

    result, detail = _relay_row(db)
    assert result == "fail", f"result={result!r} detail={detail!r}"
    assert "KOSMUYOR" in detail, detail
    assert "critical" in [s for s, _ in _backup_events(db)], _backup_events(db)


def test_relay_compares_local_date_not_utc() -> None:
    """ts karsilastirmasi YEREL tarihle yapilmali.

    VPS de klipper de Europe/Istanbul (+03) ve backup.sh ts'i yerel yaziyor;
    `date -u` ile kiyas 21:00-00:00 penceresinde bir gun kaydirip sahte
    "stale" uretirdi. mtime guard'i (`date -d 'today 02:55'`) zaten yereldi.
    """
    src = SCRIPT.read_text()
    assert "today=$(date +%Y-%m-%d)" in src, "relay yerel tarih kullanmiyor"
    assert "today=$(date -u +%Y-%m-%d)" not in src, "UTC kiyasi hala duruyor"
