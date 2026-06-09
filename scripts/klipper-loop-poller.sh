#!/usr/bin/env bash
# klipper-loop-poller.sh — /loop ile kullanilir. Her 10s nudge+API kontrol.
# Kullanim: /loop /opt/linux-ai-server/scripts/klipper-loop-poller.sh
set -uo pipefail
NUDGE_FLAG=/tmp/klipper-nudge-pending
MEM_API=http://127.0.0.1:8420/api/v1/memory
ENV_FILE=/opt/linux-ai-server/.env
STATE=/opt/linux-ai-server/data/hook-state/loop-poller-state.json
FILTER_SCRIPT=/opt/linux-ai-server/scripts/_loop_poller_filter.py

MEM_KEY="${MEMORY_API_KEY:-}"
[ -z "$MEM_KEY" ] && MEM_KEY=$(grep '^MEMORY_API_KEY=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)

LAST_ID=0
[ -f "$STATE" ] && LAST_ID=$(python3 -c "import json; print(json.load(open('$STATE')).get('last_seen_id',0))" 2>/dev/null || echo 0)
BASELINE="$LAST_ID"

while true; do
    # 1. Nudge flag — DISCUSSION notlari icin anlik uyari
    if [ -f "$NUDGE_FLAG" ]; then
        CONTENT=$(cat "$NUDGE_FLAG" 2>/dev/null)
        rm -f "$NUDGE_FLAG"
        echo "$CONTENT"
        exit 0
    fi

    # 2. API: yeni surer notu (ACTIONABLE/INFO, okunmamis olmak zorunda degil)
    NOTES=$(curl -s -m 8 "$MEM_API/notes?limit=20" -H "X-Memory-Key: $MEM_KEY" 2>/dev/null)
    if [ -n "$NOTES" ] && [ -f "$FILTER_SCRIPT" ]; then
        RESULT=$(echo "$NOTES" | python3 "$FILTER_SCRIPT" "$BASELINE" "$STATE" 2>/dev/null)
        if [ -n "$RESULT" ]; then
            echo "$RESULT"
            exit 0
        fi
    fi

    sleep 10
done
