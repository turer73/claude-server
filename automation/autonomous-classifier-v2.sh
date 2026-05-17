#!/bin/bash
# autonomous-classifier-v2.sh — Ollama classification + CONFIDENCE scoring
#
# v1'den farkı: classifier 2 aşamalı çalışır.
#   Aşama 1: Standard 4-class label (ACK/ACTIONABLE/DISCUSSION/URGENT)
#   Aşama 2: Confidence skoru (HIGH/MEDIUM/LOW) — model rasyonalin kendiliğinden
#            açıklasın, parser CONFIDENCE: HIGH|MEDIUM|LOW satırını çek.
#
# Low confidence → autonomous-claude.sh "defer to user" karari verir.
#
# Output (stdout):
#   Line 1: LABEL (ACK|ACTIONABLE|DISCUSSION|URGENT)
#   Line 2: CONFIDENCE (HIGH|MEDIUM|LOW)
#   Line 3+: optional reason (stderr)

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
CONTENT_SHORT=$(printf '%s' "$CONTENT" | head -c 800)

# Confidence-aware prompt: hem label hem confidence istiyoruz
PROMPT="SYSTEM: You are a message router. Analyze the note and output EXACTLY this format (two lines, nothing else):

LABEL: <one of ACK|ACTIONABLE|DISCUSSION|URGENT>
CONFIDENCE: <one of HIGH|MEDIUM|LOW>

Label definitions:
ACK         - acknowledgment, thanks, status update, FYI; no action needed
ACTIONABLE  - concrete tasks: commit, fix, test, deploy, count, lookup; JSON gorev_paketi
DISCUSSION  - opinion/decision/review/recommendation needed
URGENT      - security incident, KVKK breach, prod outage, hard deadline

Confidence:
HIGH    - obvious match, clear keywords/structure (e.g. title starts with 'ACK', JSON gorev_paketi, 'breach', 'KVKK')
MEDIUM  - reasonable match but some ambiguity
LOW     - genuinely ambiguous; could be 2+ categories

Rules:
- Output ONLY the two lines above, no extra text/reasoning
- If language is Turkish, classify same as English (structural intent, not language)
- If genuinely uncertain, prefer LOW confidence (autonomous router will defer to human)

Examples:
Input: 'ACK #155 - refactor live'
Output:
LABEL: ACK
CONFIDENCE: HIGH

Input: 'PR review feedback'
Output:
LABEL: DISCUSSION
CONFIDENCE: MEDIUM

Input: 'Hi'
Output:
LABEL: ACK
CONFIDENCE: LOW

--- NOTE TITLE ---
$TITLE

--- NOTE CONTENT (first 800 chars) ---
$CONTENT_SHORT

Output (two lines exactly):"

RESPONSE=$(curl -sS --max-time 20 "$OLLAMA_URL/api/generate" \
    -H "Content-Type: application/json" \
    -d "$(python3 -c "
import json, os, sys
prompt = '''$PROMPT'''
print(json.dumps({
    'model': '$OLLAMA_MODEL',
    'prompt': prompt,
    'stream': False,
    'options': {'temperature': 0.1, 'num_predict': 30}
}))
")" 2>/dev/null)

PARSED=$(printf '%s' "$RESPONSE" | python3 -c "
import json, sys, re
try:
    d = json.loads(sys.stdin.read())
    text = d.get('response', '').strip()
    # Parse LABEL + CONFIDENCE
    label = None
    confidence = None
    for line in text.split('\n'):
        line = line.strip().upper()
        if line.startswith('LABEL:'):
            v = line.split(':', 1)[1].strip()
            for c in ('URGENT', 'ACTIONABLE', 'DISCUSSION', 'ACK'):
                if c in v: label = c; break
        elif line.startswith('CONFIDENCE:'):
            v = line.split(':', 1)[1].strip()
            for c in ('HIGH', 'MEDIUM', 'LOW'):
                if c in v: confidence = c; break
    # Fallback: scan body for keywords
    if not label:
        text_upper = text.upper()
        for c in ('URGENT', 'ACTIONABLE', 'DISCUSSION', 'ACK'):
            if c in text_upper: label = c; break
    if not confidence:
        confidence = 'LOW'  # safe default if parser can't find
    if not label:
        label = 'DISCUSSION'  # safest fallback
    print(label)
    print(confidence)
except Exception as e:
    sys.stderr.write(f'parse err: {e}\n')
    print('DISCUSSION')
    print('LOW')
")

LABEL=$(printf '%s' "$PARSED" | sed -n 1p)
CONFIDENCE=$(printf '%s' "$PARSED" | sed -n 2p)

echo "$LABEL"
echo "$CONFIDENCE"
echo "classified note #$NOTE_ID as $LABEL (confidence=$CONFIDENCE, model=$OLLAMA_MODEL)" >&2
