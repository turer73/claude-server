#!/bin/bash
# notify-cron.sh — LIVESYS FAZ3.2 step-d: events tablosu pending -> n8n bildirimi.
# Author: surer (draft) + klipper (cross-verify fix'leri: obs-1/2/3, #99772).
#
# DISABLED-first: .env'de NOTIFY_CRON_ENABLED=true ayarlanana kadar calismaz.
# Cadence: */20 (automation/crontab'da kayitli — enable'da uncomment).
#
# ATOMIK CUTOVER: NOTIFY_CRON_ENABLED=true aninda klipper-cron-wrap direkt n8n POST'u
# + backup-monitor send_telegram durur; bu script devralir (DOUBLE-yok).
#
# SEND-THEN-MARK: mark_notified SADECE basarili HTTP 200 sonrasi -> at-least-once
# (fail -> mark-YOK -> sonraki run retry -> NO-LOSS).
#
# NO-LOSS (obs-3 fix, surer #99772): rolling age-skip YOK. notify-cron downtime'da
# biriken event'ler cron geri gelince TESLIM edilir (perpetual-skip = sessiz-kayip
# olurdu). Ilk-enable-flood'u ONE-TIME cutoff onler (enable-prosedürü, asagi):
#   sqlite3 server.db "UPDATE events SET notified=1 WHERE notified=0 AND severity
#     IN ('warn','critical') AND timestamp < datetime('now');"   # enable-aninda 1x
set +e

# obs-2 fix: source-.env TUM secret'i (TELEGRAM_BOT_TOKEN vb, #513) yuklerdi ->
# sadece gerekli degiskenleri scope'la oku (gereksiz expose yok).
_envget() { grep -E "^$1=" /opt/linux-ai-server/.env 2>/dev/null | head -1 | cut -d= -f2- | tr -d "\"'"; }

# env-var override > .env-dosyasi (test deterministik + prod-cron .env'den okur).
NOTIFY_CRON_ENABLED="${NOTIFY_CRON_ENABLED:-$(_envget NOTIFY_CRON_ENABLED)}"
[ "${NOTIFY_CRON_ENABLED:-false}" = "true" ] || exit 0

DB_PATH="$(_envget DB_PATH)"; DB_PATH="${DB_PATH:-/opt/linux-ai-server/data/server.db}"
N8N_WEBHOOK="$(_envget N8N_WEBHOOK_URL)"; N8N_WEBHOOK="${N8N_WEBHOOK:-http://localhost:5678/webhook/klipper-alert}"
# WEBHOOK_SECRET: cron-wrap ile AYNI webhook+secret (klipper-alert) — tutarli auth.
WEBHOOK_SECRET="$(_envget WEBHOOK_SECRET)"
LOG="/var/log/linux-ai-server/notify-cron.log"

mkdir -p "$(dirname "$LOG")" 2>/dev/null
[ -f "$DB_PATH" ] || { echo "[$(date -Iseconds)] DB not found: $DB_PATH" >> "$LOG"; exit 0; }

# pending: warn/critical, notified=0, FIFO (en eski once)
IDS=$(sqlite3 "$DB_PATH" \
    "SELECT id FROM events WHERE severity IN ('warn','critical') AND notified=0 ORDER BY id ASC;" \
    2>/dev/null)
[ -z "$IDS" ] && exit 0

echo "[$(date -Iseconds)] notify-cron: pending events — processing..." >> "$LOG"
sent=0; failed=0

for id in $IDS; do
    [ -z "$id" ] && continue
    # obs-1 fix: 6 ayri sqlite3 yerine TEK SELECT (US sep=0x1f -> bash-parse).
    row=$(sqlite3 -separator $'\x1f' "$DB_PATH" \
        "SELECT type,source,severity,title,COALESCE(detail,''),timestamp FROM events WHERE id=${id};" \
        2>/dev/null)
    [ -z "$row" ] && continue
    IFS=$'\x1f' read -r type src sev title detail ts <<< "$row"

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
        # SEND-THEN-MARK: send basarili -> mark (fail -> mark-yok -> retry, no-loss)
        sqlite3 "$DB_PATH" "UPDATE events SET notified=1 WHERE id=${id};" 2>>"$LOG" || true
        echo "[$(date -Iseconds)] SENT id=${id} src=${SAFE_SRC} sev=${sev} http=${HTTP}" >> "$LOG"
        sent=$((sent + 1))
    else
        echo "[$(date -Iseconds)] FAIL id=${id} src=${SAFE_SRC} sev=${sev} http=${HTTP} — retry next run" >> "$LOG"
        failed=$((failed + 1))
    fi
    sleep 1
done

echo "[$(date -Iseconds)] notify-cron done: sent=${sent} failed=${failed}" >> "$LOG"
exit 0
