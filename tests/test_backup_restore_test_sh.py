"""backup-restore-test.sh davranis testi — ".db" ADI SQLite kaniti degildir.

Repro (2026-08-15, disc#1551): script tarball icindeki her `*.db` dosyasina
`sqlite3 PRAGMA integrity_check` kosuyordu. `data/hook-state/` altindaki playbook
cooldown damgalari HEDEF ADIYLA isimlendiriliyor — ornegin
`investigate-db-integrity_server.db` aslinda 18 baytlik bir unix timestamp.
Bu dosya gunluk restore-test'i KALICI kirmiziya cekti (alarm korlugu riski):
gercek yedek saglamdi ama test "1/7 DB bozuk" diyordu.

Fix: ada degil ICERIGE bak — SQLite dosyasi "SQLite format 3\\0" ile baslar.
Bu test hem pozitifi (gercek DB dogrulanir) hem negatifi (sahte .db atlanir ve
SESSIZCE degil, sayilarak raporlanir) kilitler.

Not: shell-harness testlerinde CI-only-fail sinifi (locale/env/eksik-dosya) daha
once yasandi; bu yuzden script'e disaridan BACKUP_DIR + RESTORE_TEST_LOG veriliyor
ve stdout/stderr hata halinde assert mesajina gomuluyor.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "automation" / "backup-restore-test.sh"


def _make_sqlite(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (a INTEGER)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()


def _build_backup(tmp_path: Path, payload: Path) -> Path:
    """payload dizinini backup dizinine tar.gz olarak paketle, dizini dondur."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    with tarfile.open(backup_dir / "backup_test.tar.gz", "w:gz") as tar:
        tar.add(payload, arcname="data")
    return backup_dir


def _run(backup_dir: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "BACKUP_DIR": str(backup_dir),
            "RESTORE_TEST_LOG": str(tmp_path / "restore-test.log"),
        },
    )


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI yok")
def test_non_sqlite_db_named_file_is_skipped_not_failed(tmp_path: Path) -> None:
    """Adi .db ama icerigi SQLite olmayan dosya testi DUSURMEZ, atlanir."""
    payload = tmp_path / "payload"
    (payload / "hook-state").mkdir(parents=True)
    _make_sqlite(payload / "server.db")
    # Gercek tuzak: playbook cooldown damgasi, hedef adiyla isimlendirilmis.
    (payload / "hook-state" / "investigate-db-integrity_server.db").write_text("1786790404.9905772")

    result = _run(_build_backup(tmp_path, payload), tmp_path)

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "OUTCOME: pass" in result.stdout, result.stdout
    # Sessiz eleme YOK — atlanan sayisi raporlanmali.
    assert "1 atlandi" in result.stdout, result.stdout
    assert "1 DB integrity OK" in result.stdout, result.stdout


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI yok")
def test_genuinely_corrupt_sqlite_still_fails(tmp_path: Path) -> None:
    """Filtre gercek bozulmayi maskelemiyor — SQLite basligi olan bozuk dosya FAIL."""
    payload = tmp_path / "payload"
    payload.mkdir()
    db = payload / "server.db"
    # Cok sayfali olacak kadar veri: tek sayfalik DB'de ortadaki baytlari ezmek
    # integrity_check'i TETIKLEMIYOR (kullanilmayan alana denk geliyor) — ilk
    # denemede test bu yuzden yanlis-yesil verdi.
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    con.executemany("INSERT INTO t VALUES (?, ?)", [(i, "x" * 200) for i in range(500)])
    con.commit()
    con.close()

    # SQLite basligini KORU (filtre bu dosyayi atlamamali), govdeyi boz.
    raw = bytearray(db.read_bytes())
    assert raw[:15] == b"SQLite format 3"
    assert len(raw) > 16384, f"DB cok kucuk, cok-sayfali degil: {len(raw)}"
    mid = len(raw) // 2
    raw[mid : mid + 2048] = b"\xff" * 2048
    db.write_bytes(bytes(raw))

    result = _run(_build_backup(tmp_path, payload), tmp_path)

    assert result.returncode == 1, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "OUTCOME: fail" in result.stdout, result.stdout
