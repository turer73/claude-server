"""daily-backup.sh — auth dustugunde NEDENI ayirt etmeli.

Repro (2026-08-31 / 09-02): `server.db` bozulunca auth dustu (auth `api_keys`'i
oradan okuyor) ama script kosulsuz "API auth basarisiz (servis down olabilir)"
diyordu. Servis AYAKTAYDI. Bu yanlis teshis 45 saat boyunca yanlis yone baktirdi
— gercek neden bozuk DB'ydi.

Iki degisken AYRI olculmeli: servis yanit veriyor mu (/health) ve server.db
saglam mi (quick_check). Bu testler ucu de kilitler:
  - servis olu            -> "servis yanit vermiyor"
  - servis ayakta+DB bozuk-> "server.db BOZUK"
  - servis ayakta+DB saglam-> "auth anahtar sorunu"

Not: shell-harness'ta CI-only-fail sinifi (locale/env/eksik-dosya) yasandi; bu
yuzden env acikca verilir ve hata halinde stdout/stderr assert mesajina gomulur.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "automation" / "daily-backup.sh"


def _stub_bin(tmp_path: Path, health_code: str) -> Path:
    """curl stub: /health icin verilen kodu doner, auth icin BOS govde (token yok)."""
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/bin/bash\n"
        'for a in "$@"; do\n'
        '  case "$a" in\n'
        f'    */health) printf "%s" "{health_code}"; exit 0 ;;\n'
        "    https://api.telegram.org/*) exit 0 ;;\n"
        "  esac\n"
        "done\n"
        # auth cagrisi: gecersiz JSON degil, access_token'siz JSON -> TOKEN bos
        'printf "%s" "{}"\n'
    )
    curl.chmod(0o755)
    return bin_dir


def _make_db(path: Path, corrupt: bool) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    con.executemany("INSERT INTO t VALUES (?, ?)", [(i, "x" * 200) for i in range(500)])
    con.commit()
    con.close()
    if corrupt:
        raw = bytearray(path.read_bytes())
        mid = len(raw) // 2
        raw[mid : mid + 2048] = b"\xff" * 2048
        path.write_bytes(bytes(raw))


def _run(tmp_path: Path, health_code: str, db_corrupt: bool | None) -> subprocess.CompletedProcess[str]:
    db = tmp_path / "server.db"
    if db_corrupt is not None:
        _make_db(db, corrupt=db_corrupt)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": f"{_stub_bin(tmp_path, health_code)}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "API_KEY": "test-key",
            "SERVER_DB": str(db),
        },
    )


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI yok")
def test_corrupt_db_is_not_blamed_on_service_being_down(tmp_path: Path) -> None:
    """Servis AYAKTA ama DB bozuksa 'servis down' DEME — asil bug buydu."""
    result = _run(tmp_path, health_code="200", db_corrupt=True)

    assert result.returncode == 1, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "BOZUK" in result.stdout, result.stdout
    assert "yanit vermiyor" not in result.stdout, f"servis ayaktayken 'yanit vermiyor' denildi (yanlis teshis): {result.stdout!r}"


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI yok")
def test_service_down_is_reported_as_service_down(tmp_path: Path) -> None:
    """Ayirt etme gercek 'servis olu' halini maskelemiyor (kontrol testi)."""
    result = _run(tmp_path, health_code="000", db_corrupt=False)

    assert result.returncode == 1, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "yanit vermiyor" in result.stdout, result.stdout
    assert "BOZUK" not in result.stdout, result.stdout


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI yok")
def test_healthy_service_and_db_points_at_key_problem(tmp_path: Path) -> None:
    """Servis ayakta + DB saglam ise sorun anahtardadir; oyle raporlanmali."""
    result = _run(tmp_path, health_code="200", db_corrupt=False)

    assert result.returncode == 1, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "anahtar sorunu" in result.stdout, result.stdout
    assert "BOZUK" not in result.stdout, result.stdout
