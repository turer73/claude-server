#!/bin/bash
# notify-cron.sh — LIVESYS FAZ3.2 step-d: events tablosu pending -> n8n bildirimi.
# STAGING: Bu dosya data/ altinda. automation/notify-cron.sh'e tasindiktan sonra calistir:
#   sudo cp /opt/linux-ai-server/data/notify-cron.sh /opt/linux-ai-server/automation/notify-cron.sh
#   sudo chmod +x /opt/linux-ai-server/automation/notify-cron.sh
#
# DISABLED-first: .env'de NOTIFY_CRON_ENABLED=true ayarlanana kadar calisma.
# Cadence: */20 (automation/crontab'da kayitli — crontab satiri da uncomment edilmeli).
#
# ATOMIK CUTOVER semantigi: .env'de NOTIFY_CRON_ENABLED=true aninda:
#   - Bu script pending events'leri n8n'e gondermeye baslar.
#   - klipper-cron-wrap direkt n8n POST'u durur (DOUBLE-yok).
#   - backup-monitor send_telegram durur (DOUBLE-yok).
#
# SEND-THEN-MARK: mark_notified SADECE basarili HTTP 200 sonrasi -> at-least-once.
# BACKLOG-SAFE: NOTIFY_MAX_AGE_HOURS'dan eski pending'ler send edilmeden mark edilir.

set +e
source /opt/linux-ai-server/.env 2>/dev/null

[ "${NOTIFY_CRON_ENABLED:-false}" = "true" ] || exit 0

DB_PATH="${DB_PATH:-/opt/linux-ai-server/data/server.db}"
N8N_WEBHOOK="${N8N_WEBHOOK_URL:-http://localhost:5678/webhook/klipper-alert}"
LOG="/var/log/linux-ai-server/notify-cron.log"
MAX_AGE_HOURS="${NOTIFY_MAX_AGE_HOURS:-4}"

mkdir -p "$(dirname "$LOG")" 2>/dev/null

[ -f "$DB_PATH" ] || { echo "[$(date -Iseconds)] DB not found: $DB_PATH" >> "$LOG"; exit 0; }

# pending_notifications: warn/critical, notified=0, en eski once (FIFO)
IDS=$(sqlite3 "$DB_PATH" \
    "SELECT id FROM events WHERE severity IN ('warn','critical') AND notified=0 ORDER BY id ASC;" \
    2>/dev/null)

[ -z "$IDS" ] && exit 0

echo "[$(date -Iseconds)] notify-cron: found pending events — processing..." >> "$LOG"

sent=0; skipped=0; failed=0

for id in $IDS; do
    [ -z "$id" ] && continue

    type=$(sqlite3   "$DB_PATH" "SELECT type      FROM events WHERE id=${id};" 2>/dev/null)
    src=$(sqlite3    "$DB_PATH" "SELECT source    FROM events WHERE id=${id};" 2>/dev/null)
    sev=$(sqlite3    "$DB_PATH" "SELECT severity  FROM events WHERE id=${id};" 2>/dev/null)
    title=$(sqlite3  "$DB_PATH" "SELECT title     FROM events WHERE id=${id};" 2>/dev/null)
    detail=$(sqlite3 "$DB_PATH" "SELECT detail    FROM events WHERE id=${id};" 2>/dev/null)
    ts=$(sqlite3     "$DB_PATH" "SELECT timestamp FROM events WHERE id=${id};" 2>/dev/null)

    # BACKLOG-SAFE: eski event'leri send etme, mark_notified yap
    age_h=$(python3 -c "
from datetime import datetime, timezone
try:
    ts = datetime.fromisoformat('${ts}')
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    print(int((datetime.now(timezone.utc) - ts).total_seconds() / 3600))
except Exception:
    print(0)
" 2>/dev/null)

    if [ "${age_h:-0}" -gt "${MAX_AGE_HOURS}" ]; then
        sqlite3 "$DB_PATH" \
            "UPDATE events SET notified=1 WHERE id=${id};" 2>>"$LOG" || true
        echo "[$(date -Iseconds)] SKIP_OLD id=${id} age=${age_h}h src=${src}" >> "$LOG"
        skipped=$((skipped+1))
        continue
    fi

    # n8n klipper-alert payload (field uyumu: alert.source/severity/message/value/threshold)
    SAFE_TITLE=$(printf '%s' "$title"  | tr -d '"\\' | tr '\n\r\t' '   ' | head -c 200)
    SAFE_DETAIL=$(printf '%s' "$detail" | tr -d '"\\' | tr '\n\r\t' '   ' | head -c 300)
    SAFE_SRC=$(printf '%s'   "$src"    | tr -d '"\\' | head -c 80)
    SAFE_TYPE=$(printf '%s'  "$type"   | tr -d '"\\' | head -c 80)
    BODY="{\"alert\":{\"source\":\"${SAFE_SRC}\",\"severity\":\"${sev}\",\"message\":\"${SAFE_TITLE}\",\"value\":0,\"threshold\":0},\"meta\":{\"type\":\"${SAFE_TYPE}\",\"event_id\":${id},\"detail\":\"${SAFE_DETAIL}\",\"notifier\":\"notify-cron\",\"timestamp\":\"${ts}\"}}"

    HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
        -X POST "$N8N_WEBHOOK" \
        -H "Content-Type: application/json" \
        -H "X-Webhook-Secret: ${WEBHOOK_SECRET:-MISSING}" \
        -d "$BODY" 2>/dev/null)

    if [ "$HTTP" = "200" ]; then
        # SEND-THEN-MARK: send basarili -> mark_notified
        sqlite3 "$DB_PATH" \
            "UPDATE events SET notified=1 WHERE id=${id};" 2>>"$LOG" || true
        echo "[$(date -Iseconds)] SENT id=${id} src=${SAFE_SRC} sev=${sev} http=${HTTP}" >> "$LOG"
        sent=$((sent+1))
    else
        echo "[$(date -Iseconds)] FAIL id=${id} src=${SAFE_SRC} sev=${sev} http=${HTTP} — retry next run" >> "$LOG"
        failed=$((failed+1))
    fi

    sleep 1
done

echo "[$(date -Iseconds)] notify-cron done: sent=${sent} skipped=${skipped} failed=${failed}" >> "$LOG"
exit 0
