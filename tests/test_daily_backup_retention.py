"""daily-backup.sh — tarball retention penceresi.

2026-09-03: 7 -> 30. Gerekce olculdu: klipper TEK diskli ve off-site kopyasi YOK
(bkz. CLAUDE.md), yani yedek yalnizca MANTIKSAL bozulmaya karsi koruyor. O yuzden
pencerenin uzunlugu = "bozulmayi fark etme suresi". Son server.db bozulmasi 45 saat
fark edilmedi; 7 gun dar. PR#378 gecelik tarball'i ~1015 MB -> ~198 MB'a dusurdugu
icin 30 kopya ~6 GB tutuyor ve lv-root'a (62 GB bos) sigiyor.

Not: shell-harness CI-only-fail sinifi (hardcode-path/env) yasandi; bu yuzden
BACKUP_DIR/BACKUP_LOG acikca enjekte edilir ve GERCEK yedek dizinine dokunulmaz.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "automation" / "daily-backup.sh"


def _stub_bin(tmp_path: Path) -> Path:
    """curl stub: auth token verir, backup/create basarili doner, Telegram yutulur."""
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/bin/bash\n"
        'for a in "$@"; do\n'
        '  case "$a" in\n'
        "    https://api.telegram.org/*) exit 0 ;;\n"
        '    */auth/token) printf "%s" \'{"access_token":"t"}\'; exit 0 ;;\n'
        '    */backup/create*) printf "%s" \'{"success":true,"filename":"backup_auto-new.tar.gz","size_bytes":1024}\'; exit 0 ;;\n'
        "  esac\n"
        "done\n"
        'printf "%s" "{}"\n'
    )
    curl.chmod(0o755)
    return bin_dir


def _run(tmp_path: Path, n_existing: int, retention: str | None) -> tuple[subprocess.CompletedProcess[str], Path]:
    backups = tmp_path / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    # mtime'i acikca sirala: `ls -t` sirasi belirsiz kalmasin (esit-mtime CI tuzagi).
    for i in range(n_existing):
        f = backups / f"backup_auto-{i:04d}.tar.gz"
        f.write_text("x")
        import os

        os.utime(f, (1_700_000_000 + i, 1_700_000_000 + i))

    env = {
        "PATH": f"{_stub_bin(tmp_path)}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "API_KEY": "test-key",
        "BACKUP_DIR": str(backups),
        "BACKUP_LOG": str(tmp_path / "backup.log"),
    }
    if retention is not None:
        env["BACKUP_RETENTION_COUNT"] = retention
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, timeout=60, env=env)
    return r, backups


def _names(d: Path) -> list[str]:
    return sorted(p.name for p in d.glob("*.tar.gz"))


def test_default_retention_keeps_30_not_7(tmp_path: Path) -> None:
    """Varsayilan pencere 30; eski 7'de kalsaydi 23 yedek fazladan silinirdi."""
    r, backups = _run(tmp_path, n_existing=35, retention=None)

    assert "OUTCOME: pass" in r.stdout, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert len(_names(backups)) == 30, _names(backups)


def test_retention_keeps_the_newest_and_drops_the_oldest(tmp_path: Path) -> None:
    """Silinen EN ESKI olmali — yanlis uctan budamak yedegi sessizce degersizlestirir."""
    r, backups = _run(tmp_path, n_existing=35, retention="30")
    kept = _names(backups)

    assert "OUTCOME: pass" in r.stdout, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert kept[0] == "backup_auto-0005.tar.gz", kept[:3]  # 0000-0004 (en eski 5) gitti
    assert kept[-1] == "backup_auto-0034.tar.gz", kept[-3:]  # en yeni duruyor


def test_retention_is_configurable(tmp_path: Path) -> None:
    """Pencere env ile daraltilabilir (disk baskisinda geri alma yolu acik kalsin)."""
    r, backups = _run(tmp_path, n_existing=12, retention="5")

    assert "OUTCOME: pass" in r.stdout, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert len(_names(backups)) == 5, _names(backups)


def test_under_the_window_nothing_is_deleted(tmp_path: Path) -> None:
    """Pencerenin altindayken hicbir sey silinmez (off-by-one koruması)."""
    r, backups = _run(tmp_path, n_existing=3, retention="30")

    assert "OUTCOME: pass" in r.stdout, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert len(_names(backups)) == 3, _names(backups)
