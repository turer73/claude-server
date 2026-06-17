#!/bin/bash
# renderhane process-webhooks cron — 5 dakikada bir webhook kuyruğunu işler.
# surer note #100004 (2026-06-17): PR#19 vercel.json'dan cron kaldırınca Klipper üstlendi.
#
# .env gereksinimi:
#   RENDERHANE_CRON_SECRET=<Vercel env CRON_SECRET, 3d-labx-8246 paneli>
#
# Cron entry (PR#19 MERGE + Vercel deploy READY olduktan sonra ekle):
#   */5 * * * * /opt/linux-ai-server/scripts/klipper-cron-wrap.sh rh-process-webhooks \
#     /opt/linux-ai-server/automation/rh-process-webhooks.sh
#
# Test: curl -fsS -H "Authorization: Bearer $RENDERHANE_CRON_SECRET" \
#         https://www.renderhane.com/api/cron/process-webhooks
#       -> {"processed":N} beklenir; 401 = secret yanlış

set -euo pipefail
source /opt/linux-ai-server/.env 2>/dev/null

LOG=/var/log/linux-ai-server/rh-webhook-cron.log
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
ENDPOINT="https://www.renderhane.com/api/cron/process-webhooks"

if [ -z "${RENDERHANE_CRON_SECRET:-}" ]; then
    echo "[$TS] ERROR: RENDERHANE_CRON_SECRET .env'de tanımlı değil" >> "$LOG"
    exit 1
fi

RESPONSE=$(curl -fsS \
    -H "Authorization: Bearer ${RENDERHANE_CRON_SECRET}" \
    --max-time 30 \
    "$ENDPOINT" 2>&1) || {
    echo "[$TS] ERROR: curl başarısız (exit $?) — $RESPONSE" >> "$LOG"
    exit 1
}

echo "[$TS] OK: $RESPONSE" >> "$LOG"
