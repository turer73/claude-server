#!/bin/bash
# meta_cognition.sh — Meta-Cognition Agent: Düşünce kalitesini değerlendir.
# Cron wrapper: meta_cognition.py'yi çağırır.
# OUTCOME marker cron-wrap.sh tarafından okunur.

set -euo pipefail

cd /opt/linux-ai-server || exit 1

/opt/linux-ai-server/venv/bin/python3 /opt/linux-ai-server/automation/meta_cognition.py "$@"
