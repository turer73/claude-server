#!/bin/bash
# telegram-alert.sh — Reusable Telegram bot push helper
#
# Kontrat (CLI):
#   telegram-alert.sh --kind urgent_note --note-id <ID> --from <DEV> \
#                     --title <T> --preview <P> --confidence <C>
#   telegram-alert.sh --kind generic --text "<pre-formatted HTML>"
#
# Exit codes:
#   0 = sent / dedup / dry-run
#   1 = permanent fail (3 retry exhaust)
#   2 = config eksik (token/chat boş)
#   3 = bad args
#
# Env:
#   TELEGRAM_DRY_RUN=1   curl yerine [DRY] log + flag yaz
#   TELEGRAM_BOT_TOKEN   .env'den auto-load
#   TELEGRAM_CHAT_ID     .env'den auto-load
#
# Sessiz kayıp koruması: caller (autonomous-claude.sh) memory entry yazarken
# bu helper'in exit code'una bakar, fail varsa "Telegram push: FAILED" markeri
# memory'ye düşer — kullanıcı memory'den görür.

set -uo pipefail

LOG_FILE="${TELEGRAM_LOG:-/opt/linux-ai-server/data/hook-logs/autonomous-claude.log}"
ENV_FILE="${TELEGRAM_ENV_FILE:-/opt/linux-ai-server/.env}"
SENT_DIR="${TELEGRAM_SENT_DIR:-/opt/linux-ai-server/data/hook-state/telegram-sent}"
DRY_RUN="${TELEGRAM_DRY_RUN:-0}"
API_BASE="${TELEGRAM_API_BASE:-https://api.telegram.org}"

mkdir -p "$(dirname "$LOG_FILE")" "$SENT_DIR" 2>/dev/null || true

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '[%s] telegram-alert: %s\n' "$(ts)" "$*" >> "$LOG_FILE"; }

# ---------- Args ----------
KIND=""; NOTE_ID=""; FROM=""; TITLE=""; PREVIEW=""; CONFIDENCE=""; TEXT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --kind)        KIND="$2"; shift 2 ;;
        --note-id)     NOTE_ID="$2"; shift 2 ;;
        --from)        FROM="$2"; shift 2 ;;
        --title)       TITLE="$2"; shift 2 ;;
        --preview)     PREVIEW="$2"; shift 2 ;;
        --confidence)  CONFIDENCE="$2"; shift 2 ;;
        --text)        TEXT="$2"; shift 2 ;;
        *) log "bad arg: $1"; exit 3 ;;
    esac
done

case "$KIND" in
    urgent_note)
        if [ -z "$NOTE_ID" ] || [ -z "$FROM" ] || [ -z "$TITLE" ]; then
            log "missing required: note-id/from/title"; exit 3
        fi
        ;;
    generic)
        if [ -z "$TEXT" ]; then log "generic: --text bos"; exit 3; fi
        ;;
    *) log "unknown kind: $KIND"; exit 3 ;;
esac

# ---------- Env load ----------
if [ -r "$ENV_FILE" ]; then
    set -a; . "$ENV_FILE" 2>/dev/null || true; set +a
fi

TOKEN="${TELEGRAM_BOT_TOKEN:-}"
CHAT="${TELEGRAM_CHAT_ID:-}"

if [ -z "$TOKEN" ] || [ -z "$CHAT" ]; then
    log "config missing: token=$([ -n "$TOKEN" ] && echo set || echo empty) chat=$([ -n "$CHAT" ] && echo set || echo empty)"
    exit 2
fi

# ---------- Dedup (urgent_note için) ----------
FLAG_FILE=""
if [ "$KIND" = "urgent_note" ]; then
    FLAG_FILE="$SENT_DIR/$NOTE_ID"
    if [ -f "$FLAG_FILE" ]; then
        log "already-sent: note=#$NOTE_ID (flag exists)"
        exit 0
    fi
fi

# ---------- Burst marker (urgent_note için, görsel) ----------
BURST_PREFIX=""
if [ "$KIND" = "urgent_note" ]; then
    RECENT=$(find "$SENT_DIR" -maxdepth 1 -type f -newermt '60 seconds ago' 2>/dev/null | wc -l)
    if [ "$RECENT" -ge 5 ]; then
        BURST_PREFIX="[BURST $((RECENT+1))/min] "
    fi
fi

# ---------- Mesaj olustur ----------
escape_html() {
    python3 -c 'import sys,html; sys.stdout.write(html.escape(sys.stdin.read()))'
}

if [ "$KIND" = "urgent_note" ]; then
    TITLE_TRUNC=$(printf '%s' "$TITLE" | python3 -c 'import sys; s=sys.stdin.read(); sys.stdout.write(s[:120])')
    PREVIEW_TRUNC=$(printf '%s' "$PREVIEW" | python3 -c 'import sys; s=sys.stdin.read(); sys.stdout.write(s[:500])')
    TITLE_ESC=$(printf '%s' "$TITLE_TRUNC" | escape_html)
    PREVIEW_ESC=$(printf '%s' "$PREVIEW_TRUNC" | escape_html)
    FROM_ESC=$(printf '%s' "$FROM" | escape_html)
    CONF_ESC=$(printf '%s' "${CONFIDENCE:-?}" | escape_html)
    BURST_ESC=$(printf '%s' "$BURST_PREFIX" | escape_html)

    MSG="<b>🚨 URGENT — Klipper Note #${NOTE_ID}</b>

<b>From:</b> ${FROM_ESC}
<b>Title:</b> ${BURST_ESC}${TITLE_ESC}
<b>Confidence:</b> ${CONF_ESC}

<b>Preview:</b>
<pre>${PREVIEW_ESC}</pre>

<i>Yapılan:</i> Otonom mod bilgi topladı, mark-read YAPMADI.

<i>İncele:</i> <code>bash /opt/linux-ai-server/scripts/claude-memory.sh notes show ${NOTE_ID}</code>"
else
    MSG="$TEXT"
fi

# ---------- Dry-run ----------
if [ "$DRY_RUN" = "1" ]; then
    log "[DRY] would send: kind=$KIND note=#${NOTE_ID:--} chars=${#MSG}"
    if [ -n "$FLAG_FILE" ]; then touch "$FLAG_FILE"; fi
    exit 0
fi

# ---------- HTTP send + retry ----------
RESP_TMP=$(mktemp)
trap 'rm -f "$RESP_TMP"' EXIT

BACKOFFS=(1 3 10)
for attempt in 1 2 3; do
    HTTP=$(curl -s -o "$RESP_TMP" -w '%{http_code}' --max-time 10 \
        -X POST "${API_BASE}/bot${TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${CHAT}" \
        --data-urlencode "parse_mode=HTML" \
        --data-urlencode "text=${MSG}" \
        2>/dev/null)
    [ -z "$HTTP" ] && HTTP="000"

    case "$HTTP" in
        200)
            log "sent: kind=$KIND note=#${NOTE_ID:--} attempt=$attempt http=200"
            if [ -n "$FLAG_FILE" ]; then touch "$FLAG_FILE"; fi
            exit 0
            ;;
        429)
            RETRY_AFTER=$(python3 -c "import json; print(json.load(open('$RESP_TMP')).get('parameters',{}).get('retry_after',5))" 2>/dev/null || echo 5)
            log "rate-limit: note=#${NOTE_ID:--} attempt=$attempt retry_after=${RETRY_AFTER}s"
            sleep "$RETRY_AFTER"
            ;;
        400|401|403|404)
            DESC=$(python3 -c "import json; print(json.load(open('$RESP_TMP')).get('description','unknown'))" 2>/dev/null || echo unknown)
            log "permanent-fail: note=#${NOTE_ID:--} http=$HTTP desc=$DESC"
            exit 1
            ;;
        *)
            log "transient-fail: note=#${NOTE_ID:--} attempt=$attempt http=$HTTP"
            [ "$attempt" -lt 3 ] && sleep "${BACKOFFS[$attempt-1]}"
            ;;
    esac
done

log "exhausted: note=#${NOTE_ID:--} after 3 attempts"
exit 1
