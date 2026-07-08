#!/bin/bash
# reflection.sh — Remediation playbook başarı oranlarını analiz et.
# Cron wrapper: reflection.py'yi çağırır.
# OUTCOME marker cron-wrap.sh tarafından okunur.

set -euo pipefail

cd /opt/linux-ai-server || exit 1

/opt/linux-ai-server/venv/bin/python3 /opt/linux-ai-server/automation/reflection.py "$@"
