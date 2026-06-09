#!/usr/bin/env bash
# klipper-dispatch.sh — smart dispatcher'a gorev gonder
# Kullanim: klipper-dispatch.sh "gorev aciklamasi" "proje-adi"
# Ornek: klipper-dispatch.sh "bilge-arena layout.tsx component ekle" "bilge-arena"
set -uo pipefail

TASK="${1:-}"
PROJECT="${2:-}"
CONTEXT="${3:-}"
API_URL="http://127.0.0.1:8420/api/v1/dispatch/task"
MEM_KEY="${MEMORY_API_KEY:-}"

if [ -z "$TASK" ]; then
    echo 'Kullanim: klipper-dispatch.sh "gorev" ["proje"] ["context"]'
    exit 1
fi
if [ -z "$MEM_KEY" ]; then
    # .env'den yukle
    [ -f /opt/linux-ai-server/.env ] && source /opt/linux-ai-server/.env
    MEM_KEY="${MEMORY_API_KEY:-}"
fi
if [ -z "$MEM_KEY" ]; then
    echo 'HATA: MEMORY_API_KEY bulunamadi'
    exit 1
fi

BODY="$(python3 -c "import json,sys; print(json.dumps({'task': sys.argv[1], 'project': sys.argv[2], 'context': sys.argv[3]}))" "$TASK" "$PROJECT" "$CONTEXT")"

echo "[dispatch] Yonlendiriliyor: ${TASK:0:80}..."
RESULT=$(curl -s -X POST "$API_URL"     -H "X-Memory-Key: $MEM_KEY"     -H "Content-Type: application/json"     -d "$BODY")

ROUTED=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('routed_to','?'))" 2>/dev/null)
SUMMARY=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('summary',''))" 2>/dev/null)
NOTE_ID=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('surer_note_id',''))" 2>/dev/null)
MS=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('duration_ms','?'))" 2>/dev/null)

echo "[dispatch] Sonuc: $ROUTED | $SUMMARY | ${MS}ms"
if [ -n "$NOTE_ID" ] && [ "$NOTE_ID" != "None" ]; then
    echo "[dispatch] Surer notu gonderildi: #$NOTE_ID"
fi
