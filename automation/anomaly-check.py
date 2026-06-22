#!/usr/bin/env python3
"""Anomaly-check cron entry (gap-4) — metrics_history robust-anomali -> events-spine.

klipper-cron-wrap.sh ile periyodik cagrilir (lock + timeout cron-wrap TARAFINDA, watchdog #185
deseni). Bu entry sadece run_anomaly_check()'i cagirir; mantik + fail-safe + emit_throttled
app/core/anomaly_check.py icinde.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.anomaly_check import run_anomaly_check  # noqa: E402

if __name__ == "__main__":
    summary = run_anomaly_check()
    print(f"anomaly-check: {summary}")
    # klipper-cron-wrap.sh OUTCOME marker (cron_outcomes detay/health). run_anomaly_check
    # fail-safe → her zaman özet döner; cron-run tamamlandı = pass.
    print(
        f"OUTCOME: pass | anomalies={summary['anomalies']} emitted={summary['emitted']} "
        f"suppressed={summary['suppressed']} transient={summary['transient']}"
    )
