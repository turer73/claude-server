#!/bin/bash
# memory-archive-stale.sh -- Wrapper for /maintenance/archive-stale.
# Reads MEMORY_API_KEY from .env (no hardcoded credential in crontab).
# Logs response. Always exits 0 (cron should not get error mail).

set +e

ENV_FILE="/opt/linux-ai-server/.env"
LOG_DIR="/opt/linux-ai-server/data/hook-logs"
LOG="${LOG_DIR}/archive-stale.log"
API="http://127.0.0.1:8420/api/v1/memory/maintenance/archive-stale"

mkdir -p "$LOG_DIR" 2>/dev/null

TS=$(date -Iseconds)

KEY=""
if [ -r "$ENV_FILE" ]; then
    KEY=$(grep "^MEMORY_API_KEY=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
fi

if [ -z "$KEY" ]; then
    echo "[${TS}] FATAL: MEMORY_API_KEY not found in ${ENV_FILE}" >> "$LOG"
    exit 0
fi

RESP=$(curl -s -X POST --max-time 10 -H "X-Memory-Key: ${KEY}" "$API" 2>&1)
RC=$?

echo "[${TS}] rc=${RC} resp=${RESP}" >> "$LOG"

exit 0
