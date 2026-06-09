#!/usr/bin/env bash
# klipper-note-poller.sh — surer'dan gelen notlari otonom isle.
# systemd timer ile her 60sn'de bir calisir.
set -uo pipefail

LOG=/opt/linux-ai-server/logs/klipper-note-poller.log
STATE=/opt/linux-ai-server/data/klipper_poller_state.json
API_BASE=http://127.0.0.1:8420/api/v1/memory

# MEMORY_API_KEY: process env veya .env dosyasindan
MEM_KEY="${MEMORY_API_KEY:-}"
if [ -z "$MEM_KEY" ]; then
    ENV_FILE=$(find /opt/linux-ai-server -name ".env" -maxdepth 2 2>/dev/null | head -1)
    [ -n "$ENV_FILE" ] && MEM_KEY=$(grep '^MEMORY_API_KEY=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)
fi
[ -z "$MEM_KEY" ] && exit 0

_ts() { date '+%Y-%m-%d %H:%M:%S'; }
_log() { echo "$(_ts) $*" >> "$LOG"; }

# State: son gorulen ID
LAST_ID=0
if [ -f "$STATE" ]; then
    LAST_ID=$(python3 -c "import json; print(json.load(open('$STATE')).get('last_seen_id',0))" 2>/dev/null || echo 0)
fi

# Notlari cek
NOTES=$(curl -s -m 10 "$API_BASE/notes?limit=50" -H "X-Memory-Key: $MEM_KEY" 2>/dev/null)
[ -z "$NOTES" ] && { _log "WARN: notes API bos yanit"; exit 0; }

# Surer'dan gelen, okunmamis, yeni notlari isle (tek Python scripti)
python3 /opt/linux-ai-server/scripts/_klipper_poller_core.py \
    "$MEM_KEY" "$API_BASE" "$LAST_ID" "$STATE" "$LOG" <<< "$NOTES"
