#!/bin/bash
# UserPromptSubmit hook — oturum içi inter-device mesajlaşma
# Surer/diğer cihazlardan gelen yeni notları her user prompt'unda kontrol eder
# ve Claude'un context'ine enjekte eder. SessionStart unread'leri zaten yüklediği için
# bu hook yalnızca SessionStart sonrası gelen YENİ notları gösterir.
HOOK_NAME=user-prompt-messages
. "$(dirname "$0")/lib/common.sh"

DB="$HOOK_DB"
DEV="$HOOK_DEVICE"
STATE_DIR="${HOOK_LOG_DIR%/*}/hook-state"
mkdir -p "$STATE_DIR" 2>/dev/null || true

# DB veya sqlite3 yoksa sessizce çık (hook sessiz olmalı)
[ -r "$DB" ] || exit 0
command -v sqlite3 >/dev/null 2>&1 || exit 0

# Stdin'den session_id al
INPUT=$(cat 2>/dev/null)
SID=$(printf '%s' "$INPUT" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
    sid = (d.get("session_id") or "default")[:32]
    # Path-safe: alphanumeric + dash/underscore
    sid = "".join(c for c in sid if c.isalnum() or c in "-_")
    print(sid or "default")
except Exception:
    print("default")
' 2>/dev/null)

[ -z "$SID" ] && SID="default"
MARKER="$STATE_DIR/last-note-${SID}.txt"

# DB'deki şu anki MAX(id)
CURRENT_MAX=$(sqlite3 "$DB" "SELECT COALESCE(MAX(id), 0) FROM notes" 2>/dev/null)
CURRENT_MAX="${CURRENT_MAX:-0}"

# Marker yoksa: ilk fire — current max'i set et ve sessizce çık
# (SessionStart hook bu noktaya kadar olanları context'e zaten yükledi)
if [ ! -f "$MARKER" ]; then
  echo "$CURRENT_MAX" > "$MARKER"
  hook_log "session $SID initialized at note id=$CURRENT_MAX"
  exit 0
fi

LAST=$(cat "$MARKER" 2>/dev/null || echo 0)
LAST="${LAST:-0}"

# LAST sayısal değilse 0'a düşür
case "$LAST" in
  ''|*[!0-9]*) LAST=0 ;;
esac

# Yeni not yoksa marker'ı güncelle ve çık
if [ "$CURRENT_MAX" -le "$LAST" ]; then
  exit 0
fi

# Yeni notları çek: id > LAST, hedef bu cihaz veya broadcast (read durumuna BAKILMAZ — session marker yeterli)
# Tab-separated ile parse, içerik 400 karaktere kırp
NEW=$(sqlite3 -separator $'\t' "$DB" "
  SELECT id, from_device, COALESCE(to_device, '*'), title,
         REPLACE(REPLACE(substr(content, 1, 400), char(10), ' '), char(9), ' '),
         created_at
  FROM notes
  WHERE id > $LAST
    AND (to_device = '$DEV' OR to_device IS NULL)
  ORDER BY id ASC
  LIMIT 5
" 2>/dev/null)

if [ -z "$NEW" ]; then
  # Yeni not yok ama MAX büyüdü (başka cihaza giden notlar olabilir) — marker'ı güncelle
  echo "$CURRENT_MAX" > "$MARKER"
  exit 0
fi

# Stdout'a yaz — Claude Code UserPromptSubmit hook stdout'unu context'e ekler
echo "=== HAFIZA — Oturum Icinde Yeni Mesaj ==="
echo

COUNT=0
MAX_ID=$LAST
while IFS=$'\t' read -r ID FROM TO TITLE CONTENT TS; do
  [ -z "$ID" ] && continue
  COUNT=$((COUNT + 1))
  TARGET="${TO/\*/herkes}"
  echo "[#$ID | $TS] $FROM -> $TARGET"
  echo "  Baslik: $TITLE"
  if [ -n "$CONTENT" ]; then
    echo "  Icerik: $CONTENT"
  fi
  echo
  [ "$ID" -gt "$MAX_ID" ] && MAX_ID=$ID
done <<< "$NEW"

echo "Tam icerik icin: bash /opt/linux-ai-server/scripts/claude-memory.sh notes unread"
echo "Okundu isaretle: curl -X PUT http://127.0.0.1:8420/api/v1/memory/notes/<ID>/read -H \"X-Memory-Key: \$KEY\""

# Marker'ı güncelle
echo "$CURRENT_MAX" > "$MARKER"
hook_log "delivered $COUNT new note(s) to session $SID (last id=$MAX_ID, max=$CURRENT_MAX)"

exit 0
