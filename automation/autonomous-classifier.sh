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

PROMPT="SYSTEM: You are a message router. Classify the note into exactly one category. Output only the category word, nothing else.

Categories:
ACK         - Acknowledgement, confirmation: \"done\", \"ok\", \"received\", \"live\", \"tamam\", \"alindi\", \"tamamlandi\", \"calisiyor\"
ACTIONABLE  - Has explicit tasks: commit, fix, test, PR, implement, deploy, \"gorev paketi\", \"adimlar\", \"basari kriteri\", JSON task structure
DISCUSSION  - Needs human decision: strategy, tradeoff, review request, \"karar\", \"oneri\", \"strateji\", \"ne dusunuyorsun\"
URGENT      - Security/legal/incident: breach, KVKK, CVE, \"saldiri\", \"acil\", \"kritik\", data leak, \"madde 9\"

Rules:
- If title starts with \"ACK\" -> ACK (regardless of body)
- If body contains JSON with \"gorev_paketi\" key -> ACTIONABLE
- If title contains \"URGENT\" or \"ACIL\" -> URGENT
- When ambiguous between ACTIONABLE and DISCUSSION -> DISCUSSION (human decides)
- When ambiguous between ACK and anything else -> ACK

Examples:
Title: \"ACK #155 - refactor live\"                              -> ACK
Title: \"Gorev Paketi: bilge-arena fix\" + JSON body             -> ACTIONABLE
Title: \"Hangi mimari secmeliyiz?\"                              -> DISCUSSION
Title: \"KVKK breach tespit edildi\"                             -> URGENT
Title: \"Phase 2 kapandi - handoff\"                             -> ACK
Title: \"PR #154 review lazim\"                                  -> DISCUSSION
Title: \"fix(security): CSRF bypass\" + commit steps             -> ACTIONABLE

--- NOTE TITLE ---
$TITLE

--- NOTE CONTENT (first 300 chars) ---
$CONTENT_SHORT

Category:"

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
