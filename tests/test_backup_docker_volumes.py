"""backup-docker-volumes.sh — canli SQLite HAM kopyalanmaz, snapshot alinir.

Arka plan (disc#1559): klipper'daki CANLI n8n/grafana/uptime-kuma volume'leri hic
yedeklenmiyordu; pull-vps-backup ise VPS'te kalan OLU artiklarini cekiyordu.
Yeni script bu bosluğu kapatiyor.

Kritik davranis — bu testlerin kilitledigi sey:
Calisan bir SQLite'i ham kopyalamak (cp/tar) TUTARSIZ olabilir. WAL modunda veri
ana dosya ile -wal arasinda bolunur; ikisi FARKLI anlarda okunursa cift birbirini
tutmaz. Dogru yontem `.backup` (online backup API): SQLite tutarli tek bir nokta
uretir. Ve -wal/-shm arsive KONMAMALIDIR — snapshot alindiktan sonra bayat
kalirlar, restore'da tutarsizlik uretirler.

`docker` stub'lanarak kosar; gercek konteyner/volume gerekmez.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "automation" / "backup-docker-volumes.sh"

ROWS = 250


def _make_wal_db(path: Path) -> sqlite3.Connection:
    """WAL modunda, gercek -wal/-shm yan dosyalari olan bir DB uret.

    Baglanti ACIK dondurulur ve testin sonuna kadar acik tutulur. Sebep: son
    baglanti kapaninca SQLite otomatik checkpoint yapip -wal'i siliyor; o zaman
    test edilecek kosul (yan dosyalarla birlikte canli DB) hic olusmuyordu.
    Acik baglanti ayni zamanda GERCEKCI senaryodur — yedek, canli bir yazar
    varken aliniyor.
    """
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")  # veri -wal'da kalsin
    con.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    con.executemany("INSERT INTO t VALUES (?, ?)", [(i, "x" * 50) for i in range(ROWS)])
    con.commit()
    return con


def _stub_docker(bin_dir: Path, volume_name: str, mountpoint: Path) -> None:
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/bash\n"
        f'if [ "$1" = "volume" ] && [ "$2" = "inspect" ] && [ "$3" = "{volume_name}" ]; then\n'
        f'  printf "%s" "{mountpoint}"\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    docker.chmod(0o755)


def _run(tmp_path: Path, bin_dir: Path, volume_name: str, target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=180,
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "VOLBACKUP_TARGET": str(target),
            # Mount guard KAPATILMIYOR — yalnizca saglanabilir bir hedef veriliyor.
            "VOLBACKUP_MOUNT": "/",
            "VOLBACKUP_VOLUMES": volume_name,
        },
    )


@pytest.fixture
def prepared(tmp_path: Path):
    vol_dir = tmp_path / "voldata"
    vol_dir.mkdir()
    con = _make_wal_db(vol_dir / "app.sqlite")
    # SQLite OLMAYAN dosyalar da aynen tasinmali (config vb).
    (vol_dir / "config.json").write_text('{"k": "v"}')
    (vol_dir / "nested").mkdir()
    (vol_dir / "nested" / "notes.txt").write_text("merhaba")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub_docker(bin_dir, "testvol", vol_dir)
    try:
        yield tmp_path, bin_dir, vol_dir
    finally:
        con.close()


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI yok")
@pytest.mark.skipif(shutil.which("mountpoint") is None, reason="mountpoint yok")
def test_wal_sidecars_never_enter_archive(prepared) -> None:
    """-wal/-shm arsive KONMAZ; snapshot sonrasi bayat kalirlar."""
    tmp_path, bin_dir, vol_dir = prepared
    # Kaynakta gercekten var olduklarini once dogrula, yoksa test bosa gecerdi.
    assert (vol_dir / "app.sqlite-wal").exists(), "fixture WAL uretmedi — test anlamsiz olurdu"

    target = tmp_path / "out"
    result = _run(tmp_path, bin_dir, "testvol", target)
    assert "OUTCOME: pass" in result.stdout, f"stdout={result.stdout!r} stderr={result.stderr!r}"

    tarballs = list(target.rglob("*.tar.gz"))
    assert len(tarballs) == 1, tarballs
    with tarfile.open(tarballs[0]) as tar:
        names = tar.getnames()
    leaked = [n for n in names if n.endswith(("-wal", "-shm", "-journal"))]
    assert leaked == [], f"yan dosyalar sizmis: {leaked}"


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI yok")
@pytest.mark.skipif(shutil.which("mountpoint") is None, reason="mountpoint yok")
def test_snapshot_is_complete_and_valid(prepared, tmp_path: Path) -> None:
    """Arsivdeki DB butun ve TAM: -wal'daki veri de icinde olmali.

    Ham kopya yapan bir surum burada dusherdi: -wal atilip ana dosya kopyalansa
    satirlar eksik cikardi; ikisi ayri anlarda kopyalansa cift tutarsiz olurdu.
    """
    tp, bin_dir, vol_dir = prepared
    target = tp / "out2"
    result = _run(tp, bin_dir, "testvol", target)
    assert "OUTCOME: pass" in result.stdout, result.stdout

    tarball = next(iter(target.rglob("*.tar.gz")))
    extract = tp / "x"
    extract.mkdir()
    with tarfile.open(tarball) as tar:
        tar.extractall(extract, filter="data")

    db = extract / "app.sqlite"
    assert db.exists(), sorted(p.name for p in extract.rglob("*"))
    con = sqlite3.connect(db)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # -wal'da kalan veri de gelmis olmali.
    assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == ROWS
    con.close()

    # SQLite disi dosyalar da tasinmis olmali.
    assert (extract / "config.json").read_text() == '{"k": "v"}'
    assert (extract / "nested" / "notes.txt").read_text() == "merhaba"


@pytest.mark.skipif(shutil.which("mountpoint") is None, reason="mountpoint yok")
def test_missing_volume_is_fail_not_pass(prepared, tmp_path: Path) -> None:
    """Volume bulunamazsa 'pass' DEME — yedek yokken basari raporlamak en kotusu."""
    tp, bin_dir, _ = prepared
    result = _run(tp, bin_dir, "boyle-bir-volume-yok", tp / "out3")
    assert "OUTCOME: fail" in result.stdout, result.stdout
    assert "OUTCOME: pass" not in result.stdout, result.stdout
