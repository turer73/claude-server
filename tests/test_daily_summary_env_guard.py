"""#1248 repro — daily-summary POST-bloğu: MEMORY_API_KEY yoksa crash ETMEMELİ.

PR#261 (test_spawn_retry_dlq_poison) deseninin uyarlaması: gerçek heredoc-bloğu script'ten
çıkarıp key'siz .env ile koşturur. Base'de `[...][0]` IndexError → rc!=0 → FAIL (bug-kanıtı).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "automation" / "autonomous-daily-summary.sh"


def _extract_post_python_block() -> str:
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    starts = [i for i, ln in enumerate(lines) if "python3 <<'PY'" in ln]
    assert len(starts) == 1, f"beklenen 1 PY-heredoc, bulunan {len(starts)} (script yapısı değişti — testi güncelle)"
    body: list[str] = []
    for ln in lines[starts[0] + 1 :]:
        if ln.strip() == "PY":
            return "\n".join(body)
        body.append(ln)
    raise AssertionError("PY heredoc terminatörü bulunamadı")


def test_daily_summary_post_block_missing_key_graceful_skip(tmp_path):
    env_file = tmp_path / "env"
    env_file.write_text("OTHER_VAR=x\n", encoding="utf-8")  # MEMORY_API_KEY YOK
    r = subprocess.run(
        [sys.executable, "-c", _extract_post_python_block()],
        env={
            "HOOK_ENV_FILE": str(env_file),
            "SUMMARY": "ozet",
            "SLUG": "test-slug",
            "DATE": "2026-01-01",
            "SYSTEMROOT": "C:\\Windows",  # Windows-lokal socket/ssl importu; Linux'ta zararsız
            "PYTHONIOENCODING": "utf-8",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert r.returncode == 0, f"key-yok crash (fix-öncesi IndexError davranışı): {r.stderr[-300:]}"
    assert "ATLANDI" in r.stderr  # fail-loud stderr (heredoc 2>> log'a yönlenir — #261-P2 dersi)
