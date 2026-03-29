#!/bin/bash
# Automated health check — triggers webhook + logs result + Telegram alert
API=http://localhost:8420
LOG="/var/log/linux-ai-server/health-check.log"
STATE_FILE="/tmp/health-check-state"

# .env'den oku
if [ -f /opt/linux-ai-server/.env ]; then
  set -a; source /opt/linux-ai-server/.env; set +a
fi

RESULT=$(curl -s --max-time 10 -X POST $API/api/v1/monitor/webhooks/trigger/health_check -H 'Content-Type: application/json')
HEALTHY=$(echo $RESULT | python3 -c 'import sys,json; print(json.load(sys.stdin).get("healthy",False))' 2>/dev/null)
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

PREV_STATE=$(cat "$STATE_FILE" 2>/dev/null || echo "unknown")

if [ "$HEALTHY" = "True" ]; then
    echo "[$TIMESTAMP] Health OK" >> "$LOG"
    # Düzelme bildirimi (unhealthy → healthy geçişi)
    if [ "$PREV_STATE" = "unhealthy" ] && [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
      curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="$TELEGRAM_CHAT_ID" \
        -d parse_mode="Markdown" \
        -d text="✅ *Klipper Sunucu — Düzeldi*
Servis tekrar sağlıklı.
🕐 $(date '+%H:%M %d/%m/%Y')" > /dev/null 2>&1
    fi
    echo "healthy" > "$STATE_FILE"
else
    echo "[$TIMESTAMP] UNHEALTHY — $RESULT" >> "$LOG"
    # Telegram alert (sadece ilk düşüşte, spam olmasın)
    if [ "$PREV_STATE" != "unhealthy" ] && [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
      curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="$TELEGRAM_CHAT_ID" \
        -d parse_mode="Markdown" \
        -d text="🚨 *Klipper Sunucu — UNHEALTHY*
Servis yanıt vermiyor veya sağlıksız!
🕐 $(date '+%H:%M %d/%m/%Y')" > /dev/null 2>&1
    fi
    echo "unhealthy" > "$STATE_FILE"
    # Webhook event
    curl -s -X POST $API/api/v1/monitor/webhooks/receive \
      -H 'Content-Type: application/json' \
      -d "{\"source\": \"health-check\", \"event\": \"alert\", \"data\": {\"healthy\": false, \"timestamp\": \"$TIMESTAMP\"}}" > /dev/null 2>&1
fi
