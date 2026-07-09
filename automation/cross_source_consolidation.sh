#!/bin/bash
# cross_source_consolidation.sh — Cross-Source Consolidation: Farklı kaynaklardan öğrenmeleri birleştir.
# Cron wrapper: cross_source_consolidation.py'yi çağırır.
# OUTCOME marker cron-wrap.sh tarafından okunur.

set -euo pipefail

cd /opt/linux-ai-server || exit 1

/opt/linux-ai-server/venv/bin/python3 /opt/linux-ai-server/automation/cross_source_consolidation.py "$@"
