"""klipper-cron-wrap.sh — `NOTIFY: suppress` kontrati (Telegram-sel kok-nedeni).

REPRO (2026-09-03): liveness-check kalici-dead bir kaynagi gorunce ZATEN susmaya
karar veriyordu ("dead surüyor (tekrar-alarm yok)") ama yine de OUTCOME:partial
donduruyordu. Wrapper her non-pass OUTCOME'i kosulsuz alarma cevirdigi icin bastirma
bir ust katmanda etkisiz kaldi: 24 saatte 137 Telegram warn'i (10dk'da bir).

Bu testler, bastirmanin gercekten BILDIRIM yolunu kestigini ve outcome-kaydini
KESMEDIGINI kanitlar (kayit != bildirim).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WRAP = ROOT / "scripts" / "klipper-cron-wrap.sh"


def _mkdb(tmp_path: Path) -> Path:
    db = tmp_path / "server.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE cron_outcomes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "timestamp TEXT DEFAULT (datetime('now')), job TEXT, result TEXT, rc INTEGER, "
        "source TEXT, detail TEXT, attempt_no INTEGER DEFAULT 1)"
    )
    con.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "timestamp TEXT DEFAULT (datetime('now')), type TEXT, source TEXT, "
        "severity TEXT DEFAULT 'info', title TEXT, detail TEXT, payload TEXT, "
        "notified INTEGER DEFAULT 0, acked INTEGER DEFAULT 0)"
    )
    con.commit()
    con.close()
    return db


def _job(tmp_path: Path, name: str, body: str) -> Path:
    """OUTCOME (ve istege bagli NOTIFY) basan sahte cron-isi."""
    p = tmp_path / name
    p.write_text("#!/bin/bash\n" + body)
    p.chmod(0o755)
    return p


def _run(tmp_path: Path, db: Path, job: Path) -> tuple[str, str]:
    log_dir = tmp_path / "logs"
    env = {
        **os.environ,
        "DB_PATH": str(db),
        "LOG_DIR": str(log_dir),
        # notify-cron devraldi modu: wrapper'in legacy n8n POST'u kapali kalsin
        # (test disari cikis yapmasin).
        "NOTIFY_CRON_ENABLED": "true",
    }
    r = subprocess.run(
        ["bash", str(WRAP), "suppress-probe", str(job)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    log = log_dir / "suppress-probe.log"
    return r.stdout, (log.read_text() if log.exists() else "")


def _rows(db: Path) -> tuple[list, list]:
    con = sqlite3.connect(db)
    outcomes = con.execute("SELECT job, result FROM cron_outcomes").fetchall()
    events = con.execute("SELECT source, severity FROM events").fetchall()
    con.close()
    return outcomes, events


def test_suppress_marker_blocks_notification_but_keeps_outcome(tmp_path):
    """`NOTIFY: suppress` -> events (bildirim yolu) YAZILMAZ, cron_outcomes YAZILIR."""
    db = _mkdb(tmp_path)
    job = _job(
        tmp_path,
        "quiet.sh",
        'echo "OUTCOME: partial | dead surüyor: cron:restore-test"\necho "NOTIFY: suppress"\n',
    )
    _, log = _run(tmp_path, db, job)
    outcomes, events = _rows(db)

    assert outcomes == [("suppress-probe", "partial")], outcomes  # kayit SURUYOR
    assert events == [], events  # bildirim yolu KESILDI
    assert "NOTIFY-SUPPRESS" in log


def test_without_marker_partial_still_notifies(tmp_path):
    """Marker YOKKEN davranis DEGISMEZ: partial -> warn event (regresyon koruması)."""
    db = _mkdb(tmp_path)
    job = _job(tmp_path, "loud.sh", 'echo "OUTCOME: partial | gercek yeni sorun"\n')
    _, log = _run(tmp_path, db, job)
    outcomes, events = _rows(db)

    assert outcomes == [("suppress-probe", "partial")], outcomes
    assert events == [("cron:suppress-probe", "warn")], events
    assert "NOTIFY-SUPPRESS" not in log


def test_suppress_marker_does_not_mask_a_fail(tmp_path):
    """Bastirma yalniz isin KENDI kararidir; fail'de de kayit surer, event kesilir.

    Kritik olan: bastirma OUTCOME'i degistirmez -> dashboard/liveness 'fail'i gorur.
    """
    db = _mkdb(tmp_path)
    job = _job(tmp_path, "quietfail.sh", 'echo "OUTCOME: fail | x"\necho "NOTIFY: suppress"\nexit 1\n')
    _, _ = _run(tmp_path, db, job)
    outcomes, events = _rows(db)

    assert outcomes == [("suppress-probe", "fail")], outcomes
    assert events == [], events
