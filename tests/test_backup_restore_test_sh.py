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


def _run(
    backup_dir: Path,
    tmp_path: Path,
    env_extra: dict[str, str] | None = None,
    path_prefix: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    path = "/usr/bin:/bin:/usr/local/bin"
    if path_prefix is not None:
        path = f"{path_prefix}:{path}"
    env = {
        "PATH": path,
        "HOME": str(tmp_path),
        "BACKUP_DIR": str(backup_dir),
        "RESTORE_TEST_LOG": str(tmp_path / "restore-test.log"),
        "RESTORE_TEST_WORKDIR": str(tmp_path / "workdir"),
    }
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def _stub_tar(tmp_path: Path, body: str) -> Path:
    """PATH'in basina sahte `tar` koy — gercek ENOSPC'i root'suz uretemeyiz."""
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "tar"
    stub.write_text(body)
    stub.chmod(0o755)
    return bin_dir


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


# --- "yer yok" != "arsiv bozuk" (2026-09-02, yanlis-alarm) ---
#
# Repro: mktemp varsayilani /tmp = tmpfs (14G); arsiv 12.07 GB aciliyordu ->
# tar "Cannot write: Disk quota exceeded" ile duser, script bunu kosulsuz
# "tar acilamadi (corrupt?)" diye raporlardi. Yedek SAGLAMKEN bozuk sanildi
# (gzip -t rc=0, tar -tzf rc=0, diske acilis rc=0 ile kanitlandi).
# Iki ayri ariza ayni mesaji verince gercek bozulma da ayirt edilemez = alarm korlugu.


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI yok")
def test_out_of_space_reported_as_space_not_corrupt(tmp_path: Path) -> None:
    """Yer yoksa 'yer yok' denmeli — 'corrupt?' DEGIL."""
    payload = tmp_path / "payload"
    payload.mkdir()
    _make_sqlite(payload / "server.db")
    backup_dir = _build_backup(tmp_path, payload)

    bin_dir = _stub_tar(
        tmp_path,
        "#!/bin/bash\n"
        "echo 'tar: data/server.db: Cannot write: No space left on device' >&2\n"
        "echo 'tar: Exiting with failure status due to previous errors' >&2\n"
        "exit 2\n",
    )
    result = _run(backup_dir, tmp_path, path_prefix=bin_dir)

    assert result.returncode == 1, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "yer yok" in result.stdout, result.stdout
    assert "corrupt" not in result.stdout.lower(), f"yer-yok arizasi 'corrupt' diye raporlandi (alarm korlugu): {result.stdout!r}"


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI yok")
def test_genuinely_broken_archive_still_reported_as_corrupt(tmp_path: Path) -> None:
    """Ayirt etme gercek bozulmayi maskelemiyor — bozuk arsiv hala 'corrupt?'."""
    payload = tmp_path / "payload"
    payload.mkdir()
    _make_sqlite(payload / "server.db")
    backup_dir = _build_backup(tmp_path, payload)

    bin_dir = _stub_tar(
        tmp_path,
        "#!/bin/bash\necho 'tar: Unexpected EOF in archive' >&2\nexit 2\n",
    )
    result = _run(backup_dir, tmp_path, path_prefix=bin_dir)

    assert result.returncode == 1, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "corrupt" in result.stdout.lower(), result.stdout
    assert "yer yok" not in result.stdout, result.stdout


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI yok")
def test_only_db_members_are_extracted(tmp_path: Path) -> None:
    """Arsivin TAMAMI acilmamali — dogrulama zaten yalniz *.db'ye bakiyor.

    12.07 GB yerine 2.27 GB acilir; tmpfs/disk tasmasinin kaynagi buydu.
    """
    payload = tmp_path / "payload"
    payload.mkdir()
    _make_sqlite(payload / "server.db")
    (payload / "buyuk-olu-kopya.bin").write_bytes(b"\0" * (2 * 1024 * 1024))
    backup_dir = _build_backup(tmp_path, payload)

    argfile = tmp_path / "tar-args.txt"
    bin_dir = _stub_tar(
        tmp_path,
        f'#!/bin/bash\nprintf "%s\\n" "$@" > "{argfile}"\nexec /usr/bin/tar "$@"\n',
    )
    result = _run(backup_dir, tmp_path, path_prefix=bin_dir)

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    args = argfile.read_text().splitlines()
    assert "--wildcards" in args, args
    assert "*.db" in args, args


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI yok")
def test_workdir_is_configurable_and_used(tmp_path: Path) -> None:
    """Acma dizini RESTORE_TEST_WORKDIR ile diske yonlendirilebilmeli (tmpfs degil)."""
    payload = tmp_path / "payload"
    payload.mkdir()
    _make_sqlite(payload / "server.db")
    backup_dir = _build_backup(tmp_path, payload)

    workdir = tmp_path / "ozel-workdir"
    argfile = tmp_path / "tar-args.txt"
    bin_dir = _stub_tar(
        tmp_path,
        f'#!/bin/bash\nprintf "%s\\n" "$@" > "{argfile}"\nexec /usr/bin/tar "$@"\n',
    )
    result = _run(backup_dir, tmp_path, env_extra={"RESTORE_TEST_WORKDIR": str(workdir)}, path_prefix=bin_dir)

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    args = argfile.read_text().splitlines()
    target = args[args.index("-C") + 1]
    assert target.startswith(str(workdir)), f"acma dizini workdir disinda: {target}"
