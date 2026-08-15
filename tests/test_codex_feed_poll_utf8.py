"""codex-feed-poll.sh — PR basligi UTF-8 karakterinin ORTASINDAN kesilmemeli.

Repro (disc#1552): baslik `cut -c1-38` ile kirpiliyordu. GNU `cut -c` locale'den
BAGIMSIZ olarak BAYT tabanlidir (-c ile -b ayni). PR#365'in basligindaki em-dash
(U+2014 = e2 80 94) 38. bayta denk gelince ilk iki bayti kalip ucuncusu kesildi
ve cache dosyasina GECERSIZ UTF-8 yazildi.

Sonuc tuketiciye gore degisiyordu — ikisi de kotu:
  - UTF-8 locale: GNU grep dosyayi "binary" sayip HIC satir dondurmuyor ->
    agent-feed'deki Codex paneli SESSIZCE kayboluyor (hata yok, uyari yok).
  - C locale (cron/test harness): gecersiz baytlar tuketiciye ulasiyor ->
    `subprocess.run(..., text=True)` cagiran her Python tuketicisi
    UnicodeDecodeError ile patliyor.

Test cron'u taklit etmek icin LANG/LC_ALL VERMEDEN kosar — hatanin isirdigi
ortam tam olarak budur. Script'in kendi `export LC_ALL=C.UTF-8` satiri ve
yaz-once-iconv guard'i bunu kapatir.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "automation" / "codex-feed-poll.sh"

# 38. bayta em-dash denk gelsin diye gercek PR#365 basligi kullaniliyor.
TITLE = "feat(mail): Stalwart mail altyapisi — P0 kapatma, ACME/DNS-01, DKIM"


def _script_at_foreign_root(tmp_path: Path) -> Path:
    """Script'i BASKA bir kokten kosturmak icin kopyala.

    Script `cd`'i kendi konumundan turetiyor. Sabit `cd /opt/linux-ai-server`
    yazilsaydi bu kopya "OUTCOME: fail | cd" verirdi — ama gelistirme
    makinesinde /opt/linux-ai-server VAR oldugu icin yerinde kosan bir test bunu
    goremez ve yalnizca CI'da (checkout /home/runner/work/... altinda) dusertdi.
    Kopyalayarak o ortam farkini testin ICINE tasiyoruz.
    """
    root = tmp_path / "repo"
    (root / "automation").mkdir(parents=True)
    dest = root / "automation" / SCRIPT.name
    dest.write_bytes(SCRIPT.read_bytes())
    dest.chmod(0o755)
    return dest


def _stub_gh(bin_dir: Path) -> None:
    """`gh` yerine sabit cevap veren stub — ag cagrisi yok, deterministik."""
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "pr" ]; then\n'
        f"  printf '365\\t{TITLE}\\n'\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "api" ]; then\n'
        "  printf 'P1: bir sey\\nP2: baska sey\\n'\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    gh.chmod(0o755)


@pytest.mark.skipif(shutil.which("iconv") is None, reason="iconv yok")
def test_cache_is_valid_utf8_when_title_truncated_mid_character(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub_gh(bin_dir)
    out = tmp_path / "codex-open.txt"

    result = subprocess.run(
        ["bash", str(_script_at_foreign_root(tmp_path))],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            # LANG/LC_ALL BILEREK YOK — cron ortami boyle.
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CODEX_FEED_OUT": str(out),
            "HOME": str(tmp_path),
        },
    )

    assert "OUTCOME: pass" in result.stdout, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert out.exists(), "cache dosyasi yazilmadi"

    raw = out.read_bytes()
    # ASIL ISTEK: dosya gecerli UTF-8 olmali.
    raw.decode("utf-8")  # UnicodeDecodeError -> test kirilir

    # Kirpma karakter sinirinda bitmeli: yarim em-dash dizisi kalmamali.
    assert b"\xe2\x80\x94" in raw or "—" not in raw.decode("utf-8"), "em-dash ya butun olmali ya hic olmamali"
    assert b"Codex: PR#365" in raw


@pytest.mark.skipif(shutil.which("grep") is None, reason="grep yok")
def test_consumer_grep_still_sees_the_line(tmp_path: Path) -> None:
    """agent-feed.sh `grep -v '^#'` ile okuyor — gecersiz UTF-8'de GNU grep
    dosyayi binary sayip 0 satir donduruyordu (panel sessizce kayboluyordu).
    Gecerli cache'te satir gorunmeli."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub_gh(bin_dir)
    out = tmp_path / "codex-open.txt"

    subprocess.run(
        ["bash", str(_script_at_foreign_root(tmp_path))],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "CODEX_FEED_OUT": str(out), "HOME": str(tmp_path)},
    )

    seen = subprocess.run(
        ["grep", "-v", "^#", str(out)],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": os.environ["PATH"], "LC_ALL": "C.UTF-8"},
    )
    assert "Codex: PR#365" in seen.stdout, f"tuketici satiri goremedi (binary-sayildi?): stdout={seen.stdout!r}"
