#!/bin/bash
# Check system thresholds and fire alerts + Telegram
API=http://localhost:8420

# .env'den oku
if [ -f /opt/linux-ai-server/.env ]; then
  set -a; source /opt/linux-ai-server/.env; set +a
fi

METRICS=$(curl -s --max-time 10 $API/api/v1/monitor/metrics)
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

CPU=$(echo $METRICS | python3 -c 'import sys,json; print(json.load(sys.stdin).get("cpu_percent",0))')
MEM=$(echo $METRICS | python3 -c 'import sys,json; print(json.load(sys.stdin).get("memory_percent",0))')
DISK=$(echo $METRICS | python3 -c 'import sys,json; print(json.load(sys.stdin).get("disk_percent",0))')
TEMP=$(echo $METRICS | python3 -c 'import sys,json; print(json.load(sys.stdin).get("temperature",0))')

ALERT=0
MSG=""

# CPU > 85%
if python3 -c "exit(0 if $CPU > 85 else 1)" 2>/dev/null; then
    MSG="$MSG CPU:${CPU}% "
    ALERT=1
fi
# Memory > 85%
if python3 -c "exit(0 if $MEM > 85 else 1)" 2>/dev/null; then
    MSG="$MSG MEM:${MEM}% "
    ALERT=1
fi
# Disk > 90%
if python3 -c "exit(0 if $DISK > 90 else 1)" 2>/dev/null; then
    MSG="$MSG DISK:${DISK}% "
    ALERT=1
fi
# Temp > 80C
if python3 -c "exit(0 if $TEMP > 80 else 1)" 2>/dev/null; then
    MSG="$MSG TEMP:${TEMP}C "
    ALERT=1
fi

if [ $ALERT -eq 1 ]; then
    echo "[$TIMESTAMP] ALERT:$MSG" >> /var/log/linux-ai-server/alerts.log
    # Telegram alert
    if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
      curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="$TELEGRAM_CHAT_ID" \
        -d parse_mode="Markdown" \
        -d text="⚠️ *Klipper Sunucu — Eşik Aşıldı*
${MSG}
🕐 $(date '+%H:%M %d/%m/%Y')" > /dev/null 2>&1
    fi
    curl -s -X POST $API/api/v1/monitor/webhooks/receive \
      -H 'Content-Type: application/json' \
      -d "{\"source\": \"alert-check\", \"event\": \"threshold_exceeded\", \"data\": {\"message\": \"$MSG\", \"timestamp\": \"$TIMESTAMP\"}}" > /dev/null 2>&1
else
    echo "[$TIMESTAMP] OK cpu:${CPU}% mem:${MEM}% disk:${DISK}% temp:${TEMP}C" >> /var/log/linux-ai-server/alerts.log
fi
