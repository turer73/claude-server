#!/usr/bin/env bash
# agent-health-report.sh — haftalık ajan-sağlık + bulgu raporu (Haiku sentez). Salt-okunur.
# klipper-cron-wrap.sh ile çağrılır (OUTCOME marker stdout'tan parse edilir).
set -euo pipefail
cd /opt/linux-ai-server
exec ./venv/bin/python3 automation/agent-health-report.py
