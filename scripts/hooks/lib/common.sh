#!/bin/bash
# Hook ortak helper — env yükleme, API client, log
# Source ile kullan: . "$(dirname "$0")/lib/common.sh"

set -u

HOOK_DB="${HOOK_DB:-/opt/linux-ai-server/data/claude_memory.db}"
HOOK_API="${HOOK_API:-http://127.0.0.1:8420/api/v1/memory}"
HOOK_DEVICE="${HOOK_DEVICE:-$(hostname)}"
HOOK_LOG_DIR="${HOOK_LOG_DIR:-/opt/linux-ai-server/data/hook-logs}"
HOOK_ENV_FILE="${HOOK_ENV_FILE:-/opt/linux-ai-server/.env}"
HOOK_AUTONOMY="${HOOK_AUTONOMY:-supervised}"  # supervised | autonomous

mkdir -p "$HOOK_LOG_DIR" 2>/dev/null || true

# .env'den MEMORY_API_KEY yükle (set edilmemişse)
if [ -z "${MEMORY_API_KEY:-}" ] && [ -r "$HOOK_ENV_FILE" ]; then
  while IFS= read -r line; do
    case "$line" in
      MEMORY_API_KEY=*) export MEMORY_API_KEY="${line#MEMORY_API_KEY=}"; break;;
    esac
  done < "$HOOK_ENV_FILE"
fi

# Hata logu — hook'lar sessizce calismali, hatalari ayri dosyaya yazsin
hook_log() {
  local hook_name="${HOOK_NAME:-unknown}"
  printf '[%s] [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$hook_name" "$*" \
    >> "$HOOK_LOG_DIR/hooks.log" 2>/dev/null || true
}

# Memory API'ye POST — sessiz, hata varsa log'a yaz
mem_post() {
  local endpoint="$1"; shift
  local body="$1"; shift || true
  local key="${MEMORY_API_KEY:-}"
  if [ -z "$key" ]; then
    hook_log "WARN: MEMORY_API_KEY yok, $endpoint atlandi"
    return 1
  fi
  local resp
  resp=$(curl -fsS --max-time 5 \
    -H "Content-Type: application/json" \
    -H "X-Memory-Key: $key" \
    -X POST "$HOOK_API$endpoint" \
    -d "$body" 2>&1)
  local rc=$?
  if [ $rc -ne 0 ]; then
    hook_log "POST $endpoint basarisiz: $resp"
    return 1
  fi
  printf '%s' "$resp"
  return 0
}

# JSON string escape — bash icinde guvenli string -> JSON
json_escape() {
  python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' 2>/dev/null \
    || printf '"%s"' "$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/\\t/g' | tr '\n' ' ')"
}

# JSON field cek (stdin'den)
json_field() {
  local field="$1"
  python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('$field',''))" 2>/dev/null
}
