#!/bin/bash
# autonomous-classifier.sh — Note classification via local Ollama
#
# Input:  $1=note_id  $2=title  $3=content
# Output: stdout = single word: ACK | ACTIONABLE | DISCUSSION | URGENT
#         stderr = brief reason
#
# Karar mantigi:
#   ACK         = "alindi", "tesekkurler", trivial confirmation -> local mark read
#   ACTIONABLE  = somut iş (commit, fix, deploy, test, edit) -> Claude spawn
#   DISCUSSION  = goruse acik soru/oneri/review -> defer to user
#   URGENT      = KVKK deadline, security incident, prod outage -> defer + alert
#
# Local LLM kullanir (qwen2.5:7b @ localhost:11434), $0 marjinal maliyet.

set -euo pipefail

OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
OLLAMA_MODEL="${OLLAMA_CLASSIFIER_MODEL:-qwen2.5:7b}"

if [ $# -lt 3 ]; then
    echo "usage: $0 <NOTE_ID> <TITLE> <CONTENT>" >&2
    exit 2
fi

NOTE_ID="$1"
TITLE="$2"
CONTENT="$3"

# Content'i 800 char'a kirp (qwen prompt budget)
CONTENT_SHORT=$(printf '%s' "$CONTENT" | head -c 800)

PROMPT="Asagidaki notu siniflandir. Tek kelime cevap ver: ACK, ACTIONABLE, DISCUSSION, URGENT.

Tanimlar:
- ACK: alindi/tesekkurler/onay/durum guncellemesi, hicbir is gerektirmez
- ACTIONABLE: somut yapilacak is var (commit, fix, dosya edit, test cag, deploy)
- DISCUSSION: gorus istiyor, karar bekliyor, review, oneri, soru
- URGENT: KVKK deadline, security/saldiri, prod outage, hizli mudahale gerek

Note title: $TITLE

Note content:
$CONTENT_SHORT

Cevap (yalniz tek kelime):"

# Ollama API call - generate endpoint, non-streaming
RESPONSE=$(curl -sS --max-time 15 "$OLLAMA_URL/api/generate" \
    -H "Content-Type: application/json" \
    -d "$(python3 -c "
import json, os
print(json.dumps({
    'model': '$OLLAMA_MODEL',
    'prompt': '''$PROMPT'''.replace(chr(92)+chr(110), '\\n'),
    'stream': False,
    'options': {'temperature': 0.1, 'num_predict': 10}
}))
")" 2>/dev/null)

CLASSIFICATION=$(printf '%s' "$RESPONSE" | python3 -c "
import json, sys, re
try:
    d = json.loads(sys.stdin.read())
    text = d.get('response', '').strip().upper()
    # Sadece izin verilen kelimeleri bul
    for tag in ['URGENT', 'ACTIONABLE', 'DISCUSSION', 'ACK']:
        if tag in text:
            print(tag)
            sys.exit(0)
    # Default fallback
    print('DISCUSSION')
except Exception as e:
    sys.stderr.write(f'parse error: {e}\n')
    print('DISCUSSION')
")

echo "$CLASSIFICATION"
echo "classified note #$NOTE_ID as $CLASSIFICATION (model=$OLLAMA_MODEL)" >&2
