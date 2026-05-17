#!/bin/bash
# PostCompact hook — compact event'inde context summary'i memory'e [Compact] kaydet
# Compact rare (1M Opus context'te seyrek) ama olunca tum hook tracking'i sifirlanir;
# bu hook saglik agi. Surer memory_helpers.ps1 patternin bash port'u.
HOOK_NAME=post-compact-save
. "$(dirname "$0")/lib/common.sh"

# Stdin JSON: matcher (manual|auto) + summary (compact metni)
INPUT=$(cat 2>/dev/null)

SUMMARY=$(printf '%s' "$INPUT" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
    # Compact event'i summary alani veya hookSpecificOutput.summary'de gelebilir
    s = d.get("summary") or d.get("hookSpecificOutput", {}).get("summary") or ""
    # Max 2000 char (memory entry uzun olmasin)
    print(s[:2000])
except Exception:
    print("")
' 2>/dev/null)

MATCHER=$(printf '%s' "$INPUT" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get("matcher", "unknown"))
except Exception:
    print("unknown")
' 2>/dev/null)

# Bos summary -> sessizce cik (yine de marker brakcag)
if [ -z "$SUMMARY" ]; then
  hook_log "compact $MATCHER fired ama summary bos, atlandi"
  exit 0
fi

SID=$(printf '%s' "$INPUT" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
    sid = (d.get("session_id") or "unknown")[:32]
    print("".join(c for c in sid if c.isalnum() or c in "-_") or "unknown")
except Exception:
    print("unknown")
' 2>/dev/null)

TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
DATE=$(date '+%Y-%m-%d')

# Memory API'ye type=reference olarak yaz (compact summary uzun-omurlu referans)
NAME="compact-${DATE}-${SID:0:12}"
DESC="Compact event ($MATCHER) — $HOOK_DEVICE oturum $SID kismi ($TS)"

# JSON payload — python'la safe encode (SUMMARY env var olarak gec)
PAYLOAD=$(SUMMARY="$SUMMARY" MATCHER="$MATCHER" TS="$TS" NAME="$NAME" DESC="$DESC" DEV="$HOOK_DEVICE" \
  python3 -c "
import json, os
content = '[Compact ' + os.environ['MATCHER'] + ' ' + os.environ['TS'] + '] ' + os.environ['SUMMARY']
print(json.dumps({
    'type': 'reference',
    'name': os.environ['NAME'],
    'description': os.environ['DESC'],
    'content': content,
    'source_device': os.environ['DEV'],
    'rationale': 'PostCompact hook otomatik kaydetti — compact event sirasinda Claude context kaybi onlemi'
}))
" 2>/dev/null)

if [ -z "$PAYLOAD" ]; then
  hook_log "compact payload JSON encode hatasi, atlandi"
  exit 0
fi

# common.sh mem_post: ilk arg endpoint, ikinci arg body
RESPONSE=$(mem_post "/memories" "$PAYLOAD")
hook_log "compact $MATCHER saved: ${RESPONSE:0:120}"

exit 0
