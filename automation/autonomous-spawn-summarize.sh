#!/bin/bash
# autonomous-spawn-summarize.sh — Ollama-based compression of Claude spawn output
#
# Ana fikir (OpenHuman TokenJuice + agentmemory LLM compression pattern):
# Autonomous Claude spawn complete olduktan sonra spawn log JSON'undaki "result"
# Claude'un kendi rapor metni — uzun olabilir (1-3 paragraf). Bu metni Ollama
# qwen2.5:7b ile 3 cumlelik kompakt summary'e cevir + memory entry yaz.
#
# Kazanc:
#   - Kullanici sabah memory dashboard'da net "ne yapildi" goruyor
#   - Log dosyasini acmaya gerek yok (spawn-<id>.log)
#   - Token tasarrufu: Claude'un kendi memory.py POST cagrisi yapmasi gereksiz
#
# Kullanim: autonomous-spawn-summarize.sh <NOTE_ID> <SPAWN_LOG_PATH>

set -euo pipefail

LOG_FILE="${HOOK_LOG_DIR:-/opt/linux-ai-server/data/hook-logs}/autonomous-spawn-summarize.log"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
OLLAMA_MODEL="${OLLAMA_SUMMARIZER_MODEL:-qwen2.5:7b}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '[%s] %s\n' "$(ts)" "$*" >> "$LOG_FILE"; }

if [ $# -lt 2 ]; then
    log "usage: $0 <NOTE_ID> <SPAWN_LOG>"
    exit 2
fi

NOTE_ID="$1"
SPAWN_LOG="$2"

if [ ! -r "$SPAWN_LOG" ]; then
    log "spawn log not readable: $SPAWN_LOG"
    exit 0  # silently skip; not fatal
fi

# Spawn log'tan result + metadata cek
META=$(python3 -c "
import json, sys
try:
    for line in open('$SPAWN_LOG'):
        line = line.strip()
        if line.startswith('{'):
            d = json.loads(line)
            print('SUBTYPE=' + str(d.get('subtype', 'unknown')))
            print('TURNS=' + str(d.get('num_turns', 0)))
            print('DURATION_MS=' + str(d.get('duration_ms', 0)))
            print('COST=' + str(d.get('total_cost_usd', 0)))
            print('RESULT_START')
            print(d.get('result', '')[:3000])
            print('RESULT_END')
            break
except Exception as e:
    sys.stderr.write(f'parse err: {e}\n')
")

SUBTYPE=$(printf '%s' "$META" | grep '^SUBTYPE=' | cut -d= -f2-)
TURNS=$(printf '%s' "$META" | grep '^TURNS=' | cut -d= -f2-)
DURATION_MS=$(printf '%s' "$META" | grep '^DURATION_MS=' | cut -d= -f2-)
COST=$(printf '%s' "$META" | grep '^COST=' | cut -d= -f2-)
RESULT=$(printf '%s' "$META" | sed -n '/^RESULT_START$/,/^RESULT_END$/p' | sed '1d;$d')

if [ -z "$RESULT" ]; then
    log "no result in spawn log #$NOTE_ID, skip"
    exit 0
fi

# Ollama prompt: kompakt summary
PROMPT="Following is the final output of an autonomous Claude code session. The session was triggered by a single incoming note. Extract a strict 3-line summary:

LINE 1 (TASK): What was the task in 1 short sentence
LINE 2 (DONE): What was actually done in 1 short sentence (commits, files edited, tests run, deferral reason if any)
LINE 3 (OUTCOME): success | partial | deferred | failed — plus 1-clause why

Strict rules:
- Exactly 3 lines, plain text, no markdown
- Each line under 100 characters
- Output in the same language as the input (Turkish input -> Turkish output)
- No preamble, no quotation marks around lines

--- INPUT (Claude session result) ---
$RESULT

--- 3-LINE SUMMARY ---"

RESPONSE=$(curl -sS --max-time 25 "$OLLAMA_URL/api/generate" \
    -H "Content-Type: application/json" \
    -d "$(python3 -c "
import json
print(json.dumps({
    'model': '$OLLAMA_MODEL',
    'prompt': '''$PROMPT''',
    'stream': False,
    'options': {'temperature': 0.2, 'num_predict': 150}
}))
")" 2>/dev/null)

SUMMARY=$(printf '%s' "$RESPONSE" | python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    text = d.get('response', '').strip()
    # Sadece ilk 3 satir + cleanup
    lines = [l.strip() for l in text.split('\n') if l.strip()][:3]
    print('\n'.join(lines))
except Exception as e:
    sys.stderr.write(f'parse err: {e}\n')
    print('')
")

if [ -z "$SUMMARY" ]; then
    log "ollama summary empty for #$NOTE_ID"
    exit 0
fi

# Memory entry yaz
DATE_SLUG=$(date -u +%Y%m%d-%H%M)
SLUG="autonomous-spawn-${NOTE_ID}-${DATE_SLUG}"

NOTE_ID_VAR="$NOTE_ID" SLUG_VAR="$SLUG" SUMMARY_VAR="$SUMMARY" \
TURNS_VAR="$TURNS" DURATION_VAR="$DURATION_MS" COST_VAR="$COST" \
SUBTYPE_VAR="$SUBTYPE" \
python3 <<'PY'
import json, os, urllib.request
KEY = [l.split('=',1)[1].strip() for l in open(os.environ.get('HOOK_ENV_FILE', '/opt/linux-ai-server/.env')).read().splitlines() if l.startswith('MEMORY_API_KEY=')][0]
content = f"""## Autonomous Spawn Ozeti — Note #{os.environ['NOTE_ID_VAR']}

{os.environ['SUMMARY_VAR']}

---
- Turns: {os.environ['TURNS_VAR']}
- Duration: {os.environ['DURATION_VAR']} ms
- Cost (effective): ${os.environ['COST_VAR']} (Max plan kapsami)
- Subtype: {os.environ['SUBTYPE_VAR']}
"""
body = json.dumps({
    'type': 'project',
    'name': os.environ['SLUG_VAR'],
    'description': f"Autonomous spawn 3-line summary — note #{os.environ['NOTE_ID_VAR']}",
    'content': content,
    'source_device': 'klipper-autonomous',
    'rationale': 'Ollama qwen2.5:7b summarizer — Claude spawn output compression for dashboard observability'
}, ensure_ascii=False).encode('utf-8')
req = urllib.request.Request('http://127.0.0.1:8420/api/v1/memory/memories',
    data=body, method='POST',
    headers={'Content-Type':'application/json; charset=utf-8','X-Memory-Key':KEY})
try:
    resp = urllib.request.urlopen(req, timeout=5).read().decode()
    print(resp)
except Exception as e:
    print(f'write err: {e}')
PY

log "summarized #$NOTE_ID -> $SLUG (turns=$TURNS cost=\$$COST)"
exit 0
