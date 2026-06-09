#!/usr/bin/env bash
# klipper-loop-poller.sh — /loop ile kullanilir.
# Yeni surer notu veya nudge flag gelince oturumu uyandirip icerik basar.
# Kullanim: /loop /opt/linux-ai-server/scripts/klipper-loop-poller.sh
set -uo pipefail

NUDGE_FLAG=/tmp/klipper-nudge-pending
PENDING=/opt/linux-ai-server/data/hook-state/pending-notes.json
MEM_API=http://127.0.0.1:8420/api/v1/memory
ENV_FILE=/opt/linux-ai-server/.env
STATE=/opt/linux-ai-server/data/hook-state/loop-poller-state.json

get_key() {
    local k="${MEMORY_API_KEY:-}"
    [ -z "$k" ] && k=$(grep '^MEMORY_API_KEY=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)
    echo "$k"
}

MEM_KEY=$(get_key)
LAST_ID=0
[ -f "$STATE" ] && LAST_ID=$(python3 -c "import json; print(json.load(open('$STATE')).get('last_seen_id',0))" 2>/dev/null || echo 0)
BASELINE=$LAST_ID

echo "loop-poller: baseline note id=$BASELINE, interval=45s" >&2

while true; do
    # Nudge flag kontrolu (autonomous-claude.sh ya da poller yazabilir)
    if [ -f "$NUDGE_FLAG" ]; then
        CONTENT=$(cat "$NUDGE_FLAG" 2>/dev/null)
        rm -f "$NUDGE_FLAG"
        echo "== NUDGE ==
$CONTENT"
        exit 0
    fi

    # API: yeni surer notu var mi?
    NOTES=$(curl -s -m 10 "$MEM_API/notes?limit=20" -H "X-Memory-Key: $MEM_KEY" 2>/dev/null)
    if [ -n "$NOTES" ]; then
        NEW=$(python3 - "$BASELINE" << 'PY'
import sys, json
base = int(sys.argv[1])
try:
    data = json.load(sys.stdin)
    notes = data if isinstance(data, list) else (data.get('value') or data.get('notes') or [])
    fresh = [n for n in notes
             if n.get('from_device') == 'surer'
             and not n.get('read')
             and int(n.get('id', 0)) > base]
    fresh.sort(key=lambda n: n['id'])
    if fresh:
        for n in fresh:
            print(f"== NOTE #{n['id']} | {n.get('title','')} ==")
            print(n.get('content', ''))
            print()
PY
<<< "$NOTES")

        if [ -n "$NEW" ]; then
            # En buyuk ID'yi guncelle
            NEW_MAX=$(python3 - "$BASELINE" <<< "$NOTES" << 'PY2'
import sys,json
base=int(sys.argv[1])
data=json.load(sys.stdin)
notes=data if isinstance(data,list) else (data.get('value') or data.get('notes') or [])
fresh=[n for n in notes if n.get('from_device')=='surer' and not n.get('read') and int(n.get('id',0))>base]
print(max((n['id'] for n in fresh), default=base))
PY2
)
            python3 -c "import json; f=open('$STATE','w'); json.dump({'last_seen_id': $NEW_MAX}, f); f.close()" 2>/dev/null
            echo "$NEW"
            exit 0
        fi
    fi

    sleep 45
done
