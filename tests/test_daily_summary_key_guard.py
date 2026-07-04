"""#1248 repro — daily-summary python-bloğu: MEMORY_API_KEY yoksa crash ETMEMELİ.

PR#261/spawn-retry deseninin uyarlaması: gerçek-üretim python-heredoc'u
key'siz .env ile koşturur.
- Fix ÖNCESİ: `[...][0]` boş-listede IndexError → rc!=0 (bug-kanıtı).
- Fix SONRASI: fail-loud mesaj + SystemExit(0) → rc==0 + 'ATLANDI' stderr'de.
"""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "automation" / "autonomous-daily-summary.sh"


def _extract_memory_python_block() -> str:
    """'Memory'e yaz' bölümündeki tek `python3 <<'PY'` heredoc gövdesi."""
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    starts = [i for i, ln in enumerate(lines) if "python3 <<'PY'" in ln]
    assert len(starts) == 1, f"beklenen 1 PY-heredoc, bulunan {len(starts)} (script yapısı değişti — testi güncelle)"
    body: list[str] = []
    for ln in lines[starts[0] + 1 :]:
        if ln.strip() == "PY":
            return "\n".join(body)
        body.append(ln)
    raise AssertionError("PY heredoc terminatörü bulunamadı")


def test_daily_summary_block_missing_key_graceful_skip(tmp_path):
    env_file = tmp_path / "env"
    env_file.write_text("OTHER_VAR=x\n", encoding="utf-8")  # MEMORY_API_KEY YOK
    block = _extract_memory_python_block()
    r = subprocess.run(
        [sys.executable, "-c", block],
        env={
            "HOOK_ENV_FILE": str(env_file),
            "SLUG": "test-slug",
            "DATE": "2026-07-04",
            "SUMMARY": "test",
            "SYSTEMROOT": "C:\\Windows",
            "PYTHONIOENCODING": "utf-8",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert r.returncode == 0, f"key-yok crash etti (fix-öncesi IndexError davranışı): stderr={r.stderr[-400:]}"
    assert "ATLANDI" in r.stderr
