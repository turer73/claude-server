#!/usr/bin/env python3
"""Log-novelty cron entry (gap-3) — journalctl -> Drain3 -> novel-template -> events-spine.

klipper-cron-wrap.sh ile periyodik cagrilir (lock + timeout cron-wrap TARAFINDA, watchdog #185
deseni). Bu entry sadece run_log_novelty()'yi cagirir; mantik + fail-safe + KVKK-redaction +
Drain3-state app/core/log_novelty.py icinde.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.log_novelty import run_log_novelty  # noqa: E402

if __name__ == "__main__":
    summary = run_log_novelty()
    print(f"log-novelty: {summary}")
    # klipper-cron-wrap.sh OUTCOME marker (cron_outcomes detay/health). run_log_novelty
    # fail-safe → her zaman özet döner; cron-run tamamlandı = pass.
    print(
        f"OUTCOME: pass | scanned={summary['scanned']} novel={summary['novel']} "
        f"emitted={summary['emitted']} cap={summary['suppressed_cap']}"
    )
