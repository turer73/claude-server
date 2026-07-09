#!/bin/bash
# predictive_agent.sh — Predictive Agent: Gelecek sorunları tahmin et.
# Cron wrapper: predictive_agent.py'yi çağırır.
# OUTCOME marker cron-wrap.sh tarafından okunur.

set -euo pipefail

cd /opt/linux-ai-server || exit 1

/opt/linux-ai-server/venv/bin/python3 /opt/linux-ai-server/automation/predictive_agent.py "$@"
