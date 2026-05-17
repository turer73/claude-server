#!/bin/bash
# PostToolUse (Write|Edit) hook — degisen dosyalari per-session state'e yaz
# Stop hook (stop-save-session.py) bu listeyi okuyup session.files_changed alaninda saklar
# Surer memory_helpers.ps1 patternin bash port'u; eski klipper'da yoktu.
HOOK_NAME=post-edit-capture
. "$(dirname "$0")/lib/common.sh"

STATE_DIR="${HOOK_LOG_DIR%/*}/hook-state"
mkdir -p "$STATE_DIR" 2>/dev/null || true

# Stdin JSON: tool_name + tool_input.file_path + session_id
INPUT=$(cat 2>/dev/null)

# Path-safe session id (max 32 char, alphanumeric + dash/underscore)
SID=$(printf '%s' "$INPUT" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
    sid = (d.get("session_id") or "default")[:32]
    sid = "".join(c for c in sid if c.isalnum() or c in "-_")
    print(sid or "default")
except Exception:
    print("default")
' 2>/dev/null)
[ -z "$SID" ] && SID="default"

# file_path cek — Write/Edit her ikisinde de tool_input.file_path
FPATH=$(printf '%s' "$INPUT" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
    fp = d.get("tool_input", {}).get("file_path", "")
    # Newline + tab temizle (log file safety)
    fp = fp.replace("\n", " ").replace("\t", " ")
    print(fp)
except Exception:
    print("")
' 2>/dev/null)

# Bos path veya hata -> sessizce cik
[ -z "$FPATH" ] && exit 0

TOOL=$(printf '%s' "$INPUT" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get("tool_name", "?"))
except Exception:
    print("?")
' 2>/dev/null)

LOG="$STATE_DIR/edited-files-${SID}.log"
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Append satir: ISO-ts | tool | path
printf '%s\t%s\t%s\n' "$TS" "$TOOL" "$FPATH" >> "$LOG"

# FIFO: son 50 satir tut (eski satirlari at)
if [ -f "$LOG" ]; then
  LINES=$(wc -l < "$LOG" 2>/dev/null || echo 0)
  if [ "$LINES" -gt 50 ]; then
    tail -50 "$LOG" > "${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
  fi
fi

hook_log "captured $TOOL $FPATH (session $SID)"
exit 0
