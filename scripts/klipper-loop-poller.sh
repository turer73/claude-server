#!/usr/bin/env bash
# klipper-loop-poller.sh — /loop ile kullanilir.
# Yeni surer notu veya nudge flag gelince oturumu uyandirip icerik basar.
# Kullanim: /loop /opt/linux-ai-server/scripts/klipper-loop-poller.sh
set -uo pipefail

NUDGE_FLAG=/tmp/klipper-nudge-pending
MEM_API=http://127.0.0.1:8420/api/v1/memory
ENV_FILE=/opt/linux-ai-server/.env
STATE=/opt/linux-ai-server/data/hook-state/loop-poller-state.json

MEM_KEY="${MEMORY_API_KEY:-}"
[ -z "$MEM_KEY" ] && MEM_KEY=$(grep '^MEMORY_API_KEY=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)

LAST_ID=0
[ -f "$STATE" ] && LAST_ID=$(python3 -c "import json; print(json.load(open('$STATE')).get('last_seen_id',0))" 2>/dev/null || echo 0)
BASELINE="$LAST_ID"

echo "loop-poller: baseline note id=$BASELINE, interval=45s" >&2

# Yeni not filtresi — ayri python scripte tasindi (heredoc sorunu yok)
FILTER_SCRIPT=/opt/linux-ai-server/scripts/_loop_poller_filter.py

while true; do
    # 1. Nudge flag kontrolu
    if [ -f "$NUDGE_FLAG" ]; then
        CONTENT=$(cat "$NUDGE_FLAG" 2>/dev/null)
        rm -f "$NUDGE_FLAG"
        echo "== NUDGE =="
        echo "$CONTENT"
        exit 0
    fi

    # 2. API: yeni surer notu var mi?
    NOTES=$(curl -s -m 10 "$MEM_API/notes?limit=20" -H "X-Memory-Key: $MEM_KEY" 2>/dev/null)
    if [ -n "$NOTES" ] && [ -f "$FILTER_SCRIPT" ]; then
        RESULT=$(echo "$NOTES" | python3 "$FILTER_SCRIPT" "$BASELINE" "$STATE" 2>/dev/null)
        if [ -n "$RESULT" ]; then
            echo "$RESULT"
            exit 0
        fi
    fi

    sleep 45
done
