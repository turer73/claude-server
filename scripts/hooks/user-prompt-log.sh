#!/bin/bash
# UserPromptSubmit hook — kullanici niyetini local TSV'ye kaydet (rationale log).
# Otonom is yapildiginda "bu komut hangi prompt'tan geldi" sorgulanabilsin diye.
# Hook stdout'a yazmiyor — context kirletmiyoruz.
HOOK_NAME=user-prompt-log
. "$(dirname "$0")/lib/common.sh"

INPUT=$(cat)

# Stdin'i Python'a pipe ile gecir — heredoc yerine -c kullan
# Token/credential patternleri redact edilir (GitHub PAT, OpenAI, Anthropic, AWS keys).
EXTRACT=$(printf '%s' "$INPUT" | python3 -c '
import json, re, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
prompt = (d.get("prompt") or "")[:2000].replace("\t"," ").replace("\n"," ")
# Redact common credential patterns
patterns = [
    r"ghp_[A-Za-z0-9]{30,}",          # GitHub classic PAT
    r"github_pat_[A-Za-z0-9_]{30,}",  # GitHub fine-grained PAT
    r"gho_[A-Za-z0-9]{30,}",          # GitHub OAuth
    r"ghs_[A-Za-z0-9]{30,}",          # GitHub server-to-server
    r"sk-ant-[A-Za-z0-9_-]{30,}",     # Anthropic
    r"sk-[A-Za-z0-9]{30,}",           # OpenAI
    r"AKIA[0-9A-Z]{16}",              # AWS access key
]
for p in patterns:
    prompt = re.sub(p, "<REDACTED>", prompt)
sid = (d.get("session_id") or "")[:12]
cwd = d.get("cwd") or ""
print(f"{sid}\t{cwd}\t{prompt}")
' 2>/dev/null)

[ -z "$EXTRACT" ] && exit 0

LOG_FILE="$HOOK_LOG_DIR/user-prompts.tsv"
TS=$(date '+%Y-%m-%d %H:%M:%S')
printf '%s\t%s\n' "$TS" "$EXTRACT" >> "$LOG_FILE" 2>/dev/null || true

# Log dosyasi 5MB'i gectiyse rotate et
if [ -f "$LOG_FILE" ]; then
  SIZE=$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
  if [ "${SIZE:-0}" -gt 5242880 ]; then
    mv "$LOG_FILE" "$LOG_FILE.1" 2>/dev/null || true
  fi
fi

exit 0
