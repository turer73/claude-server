"""Policy-gate #1222 HOOK-katmani teslim-filtresi testleri (klipper review #100335).

Delivery-gap: teslim-filtresi yalniz list_notes API'sinde degil, HOOK-katmaninda da olmali —
aliciler notu hook'lardan (direct-SQL) aliyor, list_notes'tan DEGIL. Held-not hook'lardan
sizarsa HOLD ETKISIZ. Bu testler held'in teslim-EDILMEDIGINI ama onay-icin GORUNDUGUNU dogrular.

EN KRITIK hook (stop-check-inbox.py, otonom inbox) GERCEK subprocess ile kosulur (test-kod
divergence riski yok — 574-leak dersi); diger hook'larin ortak teslim/onay SQL-pattern'i dogrulanir.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

_STOP_HOOK = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "stop-check-inbox.py"


def _mk_notes_db(tmp_path, with_status: bool = True):
    db = tmp_path / "m.db"
    con = sqlite3.connect(db)
    cols = (
        "id INTEGER PRIMARY KEY AUTOINCREMENT, from_device TEXT, to_device TEXT, "
        "title TEXT, content TEXT, read INTEGER DEFAULT 0, read_by TEXT DEFAULT ''"
    )
    if with_status:
        cols += ", status TEXT DEFAULT 'active'"
    con.execute(f"CREATE TABLE notes ({cols})")
    return con, db


def test_stop_check_inbox_excludes_held(tmp_path):
    """EN KRITIK: otonom stop-hook inbox held-dispatch'i TESLIM ETMEZ (gercek script subprocess)."""
    con, db = _mk_notes_db(tmp_path)
    con.execute(
        "INSERT INTO notes (from_device,to_device,title,content,status) "
        "VALUES ('klipper-autonomous','surer','ACTIVE-dispatch','aktif-icerik','active')"
    )
    con.execute(
        "INSERT INTO notes (from_device,to_device,title,content,status) "
        "VALUES ('klipper-autonomous','surer','HELD-dispatch','held-icerik','held')"
    )
    con.commit()
    con.close()

    env = {
        **os.environ,
        "HOOK_DB": str(db),
        "HOOK_DEVICE": "surer",
        "HOOK_LOG_DIR": str(tmp_path / "logs"),
    }
    r = subprocess.run([sys.executable, str(_STOP_HOOK)], input="{}", capture_output=True, text=True, env=env)
    assert r.returncode == 0
    assert "ACTIVE-dispatch" in r.stdout  # active TESLIM edilir (block-reason'da gorunur)
    assert "HELD-dispatch" not in r.stdout  # held TESLIM EDILMEZ (HOLD cekirdegi)


def test_stop_check_inbox_column_guard_no_status(tmp_path):
    """Kolon-guard: status kolonu YOKSA (fresh/merge-oncesi DB) hata-VERMEZ, unread teslim (geri-uyum)."""
    con, db = _mk_notes_db(tmp_path, with_status=False)
    con.execute("INSERT INTO notes (from_device,to_device,title,content) VALUES ('k','surer','ESKI-not','x')")
    con.commit()
    con.close()

    env = {
        **os.environ,
        "HOOK_DB": str(db),
        "HOOK_DEVICE": "surer",
        "HOOK_LOG_DIR": str(tmp_path / "logs"),
    }
    r = subprocess.run([sys.executable, str(_STOP_HOOK)], input="{}", capture_output=True, text=True, env=env)
    assert r.returncode == 0
    assert "ESKI-not" in r.stdout  # status-yok -> filtre-yok, hata-yok, teslim


def test_delivery_sql_pattern_held_excluded(tmp_path):
    """Teslim-SQL-pattern (user-prompt-messages / claude-memory unread): held-HARIC, active-VAR."""
    con, _ = _mk_notes_db(tmp_path)
    con.execute("INSERT INTO notes (from_device,to_device,title,content,status) VALUES ('k','surer','A','x','active')")
    con.execute("INSERT INTO notes (from_device,to_device,title,content,status) VALUES ('k','surer','H','y','held')")
    con.commit()
    rows = con.execute(
        "SELECT title FROM notes WHERE (to_device=? OR to_device IS NULL) AND read=0 AND COALESCE(status,'active')='active' ORDER BY id",
        ("surer",),
    ).fetchall()
    con.close()
    titles = [r[0] for r in rows]
    assert "A" in titles
    assert "H" not in titles  # held teslim edilmez


def test_approval_view_sql_pattern_held_included(tmp_path):
    """session-start onay-gorunumu-SQL: held AYRI listede GORUNUR (insan approve/reject icin)."""
    con, _ = _mk_notes_db(tmp_path)
    con.execute("INSERT INTO notes (from_device,to_device,title,content,status) VALUES ('k','surer','A','x','active')")
    con.execute("INSERT INTO notes (from_device,to_device,title,content,status) VALUES ('k','surer','H','y','held')")
    con.commit()
    held = con.execute(
        "SELECT title FROM notes WHERE (to_device=? OR to_device IS NULL) AND status='held' ORDER BY id",
        ("surer",),
    ).fetchall()
    con.close()
    assert [r[0] for r in held] == ["H"]  # held onay-gorunumunde GORUNUR
