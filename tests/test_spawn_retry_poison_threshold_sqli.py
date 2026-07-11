"""#1297 (surer-doğrulama #100639) — POISON_THRESHOLD env-override doğrudan sqlite3 sorgusuna
interpolate ediliyordu ("attempt_num < $POISON_THRESHOLD"). Guard: sayısal-olmayan değer
güvenli varsayılana (3) düşer. Gerçek script içeriğinden extract edilip subprocess'te koşulur
(kopya değil) — G1 repro-gate deseni.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "automation" / "autonomous-spawn-retry.sh"


def _run_with_threshold(value: str) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index('POISON_THRESHOLD="${POISON_THRESHOLD:-3}"')
    end = text.index("INTER_SPAWN_SLEEP=", start)
    snippet = text[start:end]
    r = subprocess.run(
        ["/bin/bash", "-c", f'{snippet}\necho "$POISON_THRESHOLD"'],
        env={"POISON_THRESHOLD": value},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0, f"guard-snippet crash etti: {r.stderr}"
    return r.stdout.strip()


def test_numeric_threshold_passes_through():
    assert _run_with_threshold("7") == "7"


def test_sql_injection_payload_falls_back_to_default():
    assert _run_with_threshold("3; DROP TABLE spawn_failures;--") == "3"


def test_non_numeric_garbage_falls_back_to_default():
    assert _run_with_threshold("abc") == "3"


def test_negative_and_float_rejected():
    assert _run_with_threshold("-1") == "3"
    assert _run_with_threshold("3.5") == "3"
