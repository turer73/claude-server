#!/bin/bash
# klipper-event.sh — Klipper systemd/cron event'lerini Memory API'ye POST atan helper.
# Kullanim: klipper-event.sh <event> [details]
# Garantiler:
#   - exit 0 (systemd ExecStartPost fail-safe)
#   - Retry loop (10 deneme x 1s) — service-self-bootstrap race'ini cozer
#   - ASCII-only body (CLAUDE.md JSON kuralina uyum)
#   - Hata logu /opt/linux-ai-server/data/klipper-event.log

set +e

EVENT="${1:-unknown}"
DETAILS="${2:-}"

API="http://100.113.153.62:8420/api/v1/memory/tasks"
# .env den MEMORY_API_KEY oku (hardcoded yerine)
ENV_FILE="/opt/linux-ai-server/.env"
KEY=""
if [ -r "$ENV_FILE" ]; then
    KEY=$(grep "^MEMORY_API_KEY=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
fi
if [ -z "${KEY:-}" ]; then
    echo "[$(date -Iseconds)] FATAL: MEMORY_API_KEY not found" >> "${LOG:-/tmp/klipper-event.log}"
    exit 0
fi
LOG_DIR="/opt/linux-ai-server/data"
LOG="${LOG_DIR}/klipper-event.log"
MAX_RETRIES=10
RETRY_DELAY=1

[ -d "$LOG_DIR" ] || mkdir -p "$LOG_DIR" 2>/dev/null

# ASCII normalize: backslash, double-quote, backtick, newline stripped
TASK="systemd: ${EVENT}"
[ -n "$DETAILS" ] && TASK="${TASK} - ${DETAILS}"
TASK=$(printf "%s" "$TASK" | tr -d '\\"`' | tr '\n\r\t' '   ' | head -c 200)

BODY="{\"device_name\":\"klipper\",\"project\":\"linux-ai-server\",\"task\":\"${TASK}\",\"status\":\"completed\"}"

TS=$(date -Iseconds 2>/dev/null || date)
echo "[${TS}] event=${EVENT} task=${TASK} retry=start" >> "$LOG" 2>/dev/null

# Retry loop: API hazir olana kadar (service-start race condition fix)
HTTP=000
for i in $(seq 1 $MAX_RETRIES); do
    HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 \
        -X POST "$API" \
        -H "X-Memory-Key: ${KEY}" \
        -H "Content-Type: application/json" \
        -d "$BODY" 2>/dev/null)
    if [ "$HTTP" = "200" ] || [ "$HTTP" = "201" ]; then
        echo "[${TS}] http=${HTTP} attempt=${i}" >> "$LOG" 2>/dev/null
        break
    fi
    [ "$i" -lt "$MAX_RETRIES" ] && sleep $RETRY_DELAY
done

if [ "$HTTP" != "200" ] && [ "$HTTP" != "201" ]; then
    echo "[${TS}] http=${HTTP} attempt=${MAX_RETRIES} GIVEUP" >> "$LOG" 2>/dev/null
fi

exit 0
