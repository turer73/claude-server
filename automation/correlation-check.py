#!/usr/bin/env python3
"""Correlation-check cron entry (gap-5) — cross-source event korelasyon → incident.

klipper-cron-wrap.sh ile periyodik çağrılır (lock + timeout + DB_PATH-export cron-wrap'ta).
Bu entry sadece run_correlation_check()'i çağırır; mantık + fail-safe + emit_throttled
app/core/correlation_check.py içinde.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.correlation_check import run_correlation_check  # noqa: E402

if __name__ == "__main__":
    summary = run_correlation_check()
    print(f"correlation-check: {summary}")
    # klipper-cron-wrap.sh OUTCOME marker (cron_outcomes detay/health).
    print(
        f"OUTCOME: pass | signals={summary['signals']} incident={summary['incident']} "
        f"emitted={summary['emitted']} suppressed={summary['suppressed']}"
    )
