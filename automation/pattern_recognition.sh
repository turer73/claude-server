#!/bin/bash
# pattern-recognition.sh — Bilinç düşüncelerinde tekrar eden pattern'leri tespit et.
# Cron wrapper: pattern-recognition.py'yi çağırır.
# OUTCOME marker cron-wrap.sh tarafından okunur.

set -euo pipefail

cd /opt/linux-ai-server || exit 1

/opt/linux-ai-server/venv/bin/python3 /opt/linux-ai-server/automation/pattern_recognition.py "$@"
