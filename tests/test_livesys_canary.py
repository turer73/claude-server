"""automation/livesys-canary.sh — alarm-yolu sentetik canary (LIVESYS-SENSE PR3).

Alarm PIPELINE doğruluğunu sınar: known-good+known-bad → cron_outcomes beklenen satır.
Sağlam pipeline→pass, bozuk (wrapper yazmıyor)→fail; test-satırları temizlenir.
Gerçek klipper-cron-wrap kullanılır (CANARY_SUPPRESS_ALERT=1 → alarm tetiklemez), temp-DB.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CANARY = ROOT / "automation" / "livesys-canary.sh"
WRAP = ROOT / "scripts" / "klipper-cron-wrap.sh"

# healthy-path GERÇEK wrapper'ı çalıştırır → wrapper cron_outcomes'a sqlite3 CLI ile yazar.
# GitHub runner'da sqlite3 CLI-binary olmayabilir (python modülü ≠ CLI) → integration atlanır
# (prod klipper'da sqlite3 CLI var, canary çalışır). Diğer testler CLI gerektirmez.
_NEEDS_SQLITE3_CLI = pytest.mark.skipif(
    shutil.which("sqlite3") is None, reason="sqlite3 CLI yok (wrapper cron_outcomes yazımı için gerekli)"
)


def _mkdb(tmp_path) -> Path:
    db = tmp_path / "server.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE cron_outcomes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "timestamp TEXT DEFAULT (datetime('now')), job TEXT, result TEXT, rc INTEGER, "
        "source TEXT, detail TEXT, attempt_no INTEGER DEFAULT 1)"
    )
    con.commit()
    con.close()
    return db


def _run(db: Path, wrap: Path) -> str:
    env = {**os.environ, "DB_PATH": str(db), "WRAP": str(wrap)}
    r = subprocess.run(["bash", str(CANARY)], capture_output=True, text=True, env=env)
    return r.stdout


def _outcome(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("OUTCOME:"):
            return line
    return ""


@_NEEDS_SQLITE3_CLI
def test_canary_pass_on_healthy_pipeline(tmp_path):
    db = _mkdb(tmp_path)
    out = _outcome(_run(db, WRAP))
    assert out.startswith("OUTCOME: pass"), out
    assert "alarm-yolu sağlam" in out


def test_canary_cleans_up_its_rows(tmp_path):
    db = _mkdb(tmp_path)
    _run(db, WRAP)
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM cron_outcomes WHERE job LIKE 'livesys-canary-%'").fetchone()[0]
    con.close()
    assert n == 0  # test-satırları silindi (prod cron_outcomes kirletilmez)


def test_canary_fail_on_broken_pipeline(tmp_path):
    # cron_outcomes'a YAZMAYAN sahte wrapper → pipeline bozuk → fail
    fake = tmp_path / "fake-wrap.sh"
    fake.write_text("#!/bin/bash\nexit 0\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    db = _mkdb(tmp_path)
    out = _outcome(_run(db, fake))
    assert out.startswith("OUTCOME: fail"), out
    assert "BOZUK" in out


def test_wrapper_has_canary_suppress_guard():
    # PR3: wrapper CANARY_SUPPRESS_ALERT=1'de alert/event atlar (cron_outcomes yazılır)
    assert "CANARY_SUPPRESS_ALERT" in WRAP.read_text()


def test_canary_source_guard_fails_loud_when_lib_missing(tmp_path):
    # surer kemer-kayış: outcome.sh bulunamazsa canary SILENT-GREEN olmaz → OUTCOME: fail
    empty = tmp_path / "noroot"
    empty.mkdir()
    env = {**os.environ, "LIVESYS_ROOT": str(empty)}
    r = subprocess.run(["bash", str(CANARY)], capture_output=True, text=True, env=env)
    assert "OUTCOME: fail" in r.stdout
    assert "source-fail" in r.stdout
