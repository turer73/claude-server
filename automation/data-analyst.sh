#!/bin/bash
# Haftalık veri-analisti — scripts/data-analyst.py sarmalayıcısı (klipper-cron-wrap.sh ile
# çağrılır → OUTCOME marker'ı cron_outcomes'a düşer). Opt-in: DATA_ANALYST_ENABLED=true.
set -euo pipefail
cd /opt/linux-ai-server
exec ./venv/bin/python3 scripts/data-analyst.py
