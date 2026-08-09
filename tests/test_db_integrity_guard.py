"""Keşif 1462 — canlı-DB VACUUM kaldırıldı + bozulma detektörü + shell busy_timeout.

Repro-test (base'de FAIL):
  - test_db_retention_does_not_vacuum_live_db: base'de db-retention.sh CANLI server.db
    üzerinde VACUUM koşuyordu. VACUUM tüm B-tree/indeksleri baştan yazar; ardındaki
    wal_checkpoint(TRUNCATE) 2 uvicorn worker aktifken kilit alamayıp sessizce başarısız
    olur → WAL ~DB-boyutuna şişer, yarım kalan yeniden-inşa bozulma üretir
    ("2nd reference to page X" + "wrong # of entries in index idx_*").
    2026-08-09 canlı yakalandı; script bunu aylardır negatif "freed" olarak loglamış.
  - test_detector_flags_corrupt_db / test_detector_passes_healthy_db: base'de
    automation/db-integrity-check.sh YOK.
  - test_shell_sqlite_calls_have_busy_timeout: base'de emit-event.sh dahil shell
    yazıcıları .timeout'suzdu → kilit anında yazım sessizce düşüyordu
    (notify-cron.log'da "database is locked" canlı kanıt).
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DETECTOR = ROOT / "automation" / "db-integrity-check.sh"
RETENTION = ROOT / "automation" / "db-retention.sh"


def _run_detector(tmp_path: Path, data_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(DETECTOR)],
        env={
            "PATH": "/usr/bin:/bin",
            "DB_DATA_DIR": str(data_dir),
            "DB_INTEGRITY_LOG": str(tmp_path / "integrity.log"),
            "EMIT_EVENT_BIN": str(tmp_path / "no-such-emitter"),  # prod events'e yazma
        },
        capture_output=True,
        text=True,
        timeout=120,
    )


def _make_db(path: Path, rows: int = 400) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.execute("CREATE INDEX idx_t_v ON t(v)")
    con.executemany("INSERT INTO t (v) VALUES (?)", [(f"deger-{i}" * 8,) for i in range(rows)])
    con.commit()
    con.close()


def test_detector_passes_healthy_db(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _make_db(data / "saglikli.db")
    r = _run_detector(tmp_path, data)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OUTCOME: pass" in r.stdout
    assert "OK   saglikli.db" in (tmp_path / "integrity.log").read_text()


def test_detector_flags_corrupt_db(tmp_path):
    """Bozuk DB -> OUTCOME: fail + rc=1 + log'da BOZUK satırı (dakika hassasiyetli damga)."""
    data = tmp_path / "data"
    data.mkdir()
    db = data / "bozuk.db"
    _make_db(db)
    # Sayfa içeriğini boz: b-tree/index tutarsızlığı üret (gerçek bozulma sınıfı).
    raw = bytearray(db.read_bytes())
    page_size = int.from_bytes(raw[16:18], "big") or 4096
    for page in (2, 3):
        off = page * page_size
        if off + 64 < len(raw):
            raw[off : off + 64] = b"\xde\xad\xbe\xef" * 16
    db.write_bytes(bytes(raw))

    r = _run_detector(tmp_path, data)
    assert r.returncode == 1, f"bozuk DB fail vermedi: {r.stdout}"
    assert "OUTCOME: fail" in r.stdout
    assert "bozuk.db" in r.stdout
    assert "BOZUK bozuk.db" in (tmp_path / "integrity.log").read_text()


def test_detector_skips_corrupt_archives(tmp_path):
    """*.corrupt-* arşivleri BİLEREK atlanır — yoksa her saat yalancı alarm üretirlerdi."""
    data = tmp_path / "data"
    data.mkdir()
    _make_db(data / "canli.db")
    arsiv = data / "canli.db.corrupt-20260809-134445"
    arsiv.write_bytes(b"bu gecerli bir SQLite dosyasi degil")
    # glob *.db ile eslesmesi icin .db uzantili bir arsiv de koy
    arsiv2 = data / "eski.corrupt-20260728.db"
    arsiv2.write_bytes(b"bozuk arsiv")

    r = _run_detector(tmp_path, data)
    assert r.returncode == 0, r.stdout
    assert "OUTCOME: pass" in r.stdout
    assert "1 DB integrity OK" in r.stdout  # yalnız canli.db sayıldı


def test_db_retention_does_not_vacuum_live_db():
    """Kök-neden guard'ı: retention CANLI DB üzerinde VACUUM ÇALIŞTIRMAMALI.

    KONUM duyarlı: kelimenin geçmesi değil, ÇALIŞTIRILMASI yasak. İlk sürümüm naif
    "VACUUM not in kod" idi ve kendi log mesajıma ('VACUUM yok — keşif 1462') takıldı —
    yani testin kendisi 'mentions != does' tuzağına düştü. Ölçüt artık: aynı satırda
    hem sqlite3 çağrısı hem VACUUM.
    """
    kod = [ln for ln in RETENTION.read_text().splitlines() if not ln.lstrip().startswith("#")]
    calistiran = [ln.strip() for ln in kod if "sqlite3" in ln and re.search(r"\bVACUUM\b", ln, re.I)]
    assert not calistiran, f"db-retention.sh hâlâ canlı DB'de VACUUM koşuyor (keşif 1462): {calistiran}"
    assert any("wal_checkpoint(TRUNCATE)" in ln for ln in kod), "checkpoint kaldırılmamalı — WAL sınırsız büyür"


@pytest.mark.parametrize(
    "rel",
    [
        "scripts/emit-event.sh",
        "scripts/agent-feed.sh",
        "automation/notify-cron.sh",
        "scripts/hooks/session-start.sh",
    ],
)
def test_shell_sqlite_calls_have_busy_timeout(rel):
    """Her sqlite3 CLI çağrısı .timeout taşımalı; yoksa kilitte yazım SESSİZCE düşer.

    Python yolları (get_conn, Database) busy_timeout'u her zaman set ediyordu; shell
    yazıcıları etmiyordu — asimetri gerçek olay üretti (notify-cron 'database is locked').
    """
    src = (ROOT / rel).read_text()
    eksik = [ln.strip() for ln in src.splitlines() if not ln.lstrip().startswith("#") and re.search(r"\bsqlite3\b(?!\s+-cmd)", ln)]
    assert not eksik, f"{rel}: .timeout'suz sqlite3 çağrısı: {eksik[:3]}"
