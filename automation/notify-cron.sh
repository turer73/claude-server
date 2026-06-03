#!/bin/bash
# notify-cron.sh — LIVESYS FAZ3.2 step-d: events tablosu pending -> Telegram bildirimi.
# Author: surer (draft) + klipper (cross-verify fix'leri: obs-1/2/3, #99772).
#        2026-06-03: n8n backend -> direkt Telegram Bot API (n8n klipper'da workflow yok).
#
# DISABLED-first: .env'de NOTIFY_CRON_ENABLED=true ayarlanana kadar calismaz.
# Cadence: */20 (automation/crontab'da kayitli)
#
# ATOMIK CUTOVER: NOTIFY_CRON_ENABLED=true aninda klipper-cron-wrap direkt n8n POST'u
# + backup-monitor send_telegram durur; bu script devralir (DOUBLE-yok).
#
# SEND-THEN-MARK: mark_notified SADECE basarili HTTP 200 sonrasi -> at-least-once
# (fail -> mark-YOK -> sonraki run retry -> NO-LOSS).
set +e

_envget() { grep -E "^$1=" /opt/linux-ai-server/.env 2>/dev/null | head -1 | cut -d= -f2- | tr -d "\"'"; }

NOTIFY_CRON_ENABLED="${NOTIFY_CRON_ENABLED:-$(_envget NOTIFY_CRON_ENABLED)}"
[ "${NOTIFY_CRON_ENABLED:-false}" = "true" ] || exit 0

DB_PATH="$(_envget DB_PATH)"; DB_PATH="${DB_PATH:-/opt/linux-ai-server/data/server.db}"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-$(_envget TELEGRAM_BOT_TOKEN)}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-$(_envget TELEGRAM_CHAT_ID)}"
LOG="/var/log/linux-ai-server/notify-cron.log"

mkdir -p "$(dirname "$LOG")" 2>/dev/null
[ -f "$DB_PATH" ] || { echo "[$(date -Iseconds)] DB not found: $DB_PATH" >> "$LOG"; exit 0; }
if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ]; then
    echo "[$(date -Iseconds)] TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID eksik — bildirim ATLANAMAZ" >> "$LOG"
    exit 0
fi

TG_URL="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage"

IDS=$(sqlite3 "$DB_PATH" \
    "SELECT id FROM events WHERE severity IN ('warn','critical') AND notified=0 ORDER BY id ASC;" \
    2>/dev/null)
[ -z "$IDS" ] && exit 0

echo "[$(date -Iseconds)] notify-cron: pending events — processing..." >> "$LOG"
sent=0; failed=0

for id in $IDS; do
    [ -z "$id" ] && continue
    row=$(sqlite3 -separator $'\x1f' "$DB_PATH" \
        "SELECT type,source,severity,title,COALESCE(detail,''),timestamp FROM events WHERE id=${id};" \
        2>/dev/null)
    [ -z "$row" ] && continue
    IFS=$'\x1f' read -r type src sev title detail ts <<< "$row"

    SEV_TAG="[WARN]"; [ "$sev" = "critical" ] && SEV_TAG="[CRITICAL]"
    SAFE_TITLE=$(printf '%s' "$title"  | tr -d '<>&"' | tr '\n\r\t' '   ' | head -c 200)
    SAFE_DETAIL=$(printf '%s' "$detail" | tr -d '<>&"' | tr '\n\r\t' '   ' | head -c 300)
    SAFE_SRC=$(printf '%s' "$src" | tr -d '<>&"' | head -c 80)

    MSG="${SEV_TAG} klipper
src: ${SAFE_SRC}
${SAFE_TITLE}"
    [ -n "$SAFE_DETAIL" ] && MSG="${MSG}
${SAFE_DETAIL}"
    MSG="${MSG}
${ts}"

    JSON_MSG=$(printf '%s' "$MSG" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))")
    BODY="{\"chat_id\":\"${TELEGRAM_CHAT_ID}\",\"text\":${JSON_MSG}}"

    HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 \
        -X POST "$TG_URL" \
        -H "Content-Type: application/json" \
        -d "$BODY" 2>/dev/null)

    if [ "$HTTP" = "200" ]; then
        sqlite3 "$DB_PATH" "UPDATE events SET notified=1 WHERE id=${id};" 2>>"$LOG" || true
        echo "[$(date -Iseconds)] SENT id=${id} src=${SAFE_SRC} sev=${sev}" >> "$LOG"
        sent=$((sent + 1))
    else
        echo "[$(date -Iseconds)] FAIL id=${id} src=${SAFE_SRC} sev=${sev} http=${HTTP} — retry next run" >> "$LOG"
        failed=$((failed + 1))
    fi
    sleep 1
done

echo "[$(date -Iseconds)] notify-cron done: sent=${sent} failed=${failed}" >> "$LOG"
exit 0
