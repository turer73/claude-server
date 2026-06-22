#!/usr/bin/env python3
"""Drift-check cron entry (gap-8) — deployed≠running / config drift -> events-spine.

klipper-cron-wrap.sh ile periyodik cagrilir (lock + timeout cron-wrap TARAFINDA, watchdog #185
deseni). Bu entry sadece run_drift_check()'i cagirir; mantik + fail-safe + emit_throttled
app/core/drift_check.py icinde.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.drift_check import run_drift_check  # noqa: E402

if __name__ == "__main__":
    summary = run_drift_check()
    print(f"drift-check: {summary}")
