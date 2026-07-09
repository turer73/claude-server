#!/bin/bash
# self_improvement.sh — Self-Improvement Agent: Kendi kodunu iyileştirme önerileri üret.
# Cron wrapper: self_improvement.py'yi çağırır.
# OUTCOME marker cron-wrap.sh tarafından okunur.

set -euo pipefail

cd /opt/linux-ai-server || exit 1

/opt/linux-ai-server/venv/bin/python3 /opt/linux-ai-server/automation/self_improvement.py "$@"
