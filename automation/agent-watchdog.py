#!/usr/bin/env python3
"""Agent watchdog cron entry (gap-7) — runaway-proc + heartbeat-stall -> events-spine.

klipper-cron-wrap.sh / systemd-timer ile ~1dk caginir. Tek-instance-lock + per-run
timeout cron-wrap-TARAFINDA (mekanizma-disi 2. katman, klipper #100114/#100115).
Bu entry sadece run_watchdog()'u cagirir; FP-onleme (comert-esik+allowlist+kademeli-kill)
ve fail-safe app/core/agent_watchdog.py icinde.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.agent_watchdog import run_watchdog  # noqa: E402

if __name__ == "__main__":
    summary = run_watchdog()
    print(f"agent-watchdog: {summary}")
