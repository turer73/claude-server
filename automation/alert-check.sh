#!/bin/bash
# Check system thresholds and fire alerts.
#
# Routing (2026-05-02 sonrasi):
#   1) Birincil: n8n klipper-alert webhook -> klipper-self-healing-01 workflow
#      - severity=critical (10pp+ esik asimi) -> Agent durumu + remediate + verify + Telegram
#      - severity=warning  (esik asimi)       -> "Telegram: Uyari" (DOLU template)
#   2) Yedek: n8n erisilemezse direkt Telegram (resilience)
#   3) Daima: lokal /receive ring buffer (debug + audit)

API=http://localhost:8420
N8N_WEBHOOK="https://n8n.panola.app/webhook/klipper-alert"

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

# Esikler
T_CPU=85
T_MEM=85
T_DISK=90
T_TEMP=80
# Critical band: esik + bu kadar uzeri -> severity=critical (self-heal devreye girer)
CRITICAL_BAND=10

ALERT=0
MSG=""
SEVERITY="warning"
PRIMARY_METRIC=""
PRIMARY_VALUE=0
PRIMARY_THRESHOLD=0

check_metric() {
    local name="$1" value="$2" threshold="$3" suffix="$4"
    local breached
    breached=$(python3 -c "print(1 if $value > $threshold else 0)" 2>/dev/null)
    if [ "$breached" = "1" ]; then
        MSG="$MSG ${name}:${value}${suffix}"
        ALERT=1
        # En yuksek breach magnitude'unu primary olarak isaretle
        local diff highest
        diff=$(python3 -c "print($value - $threshold)" 2>/dev/null)
        highest=$(python3 -c "print(1 if $value > $PRIMARY_VALUE else 0)" 2>/dev/null)
        if [ "$highest" = "1" ]; then
            PRIMARY_METRIC="$name"
            PRIMARY_VALUE="$value"
            PRIMARY_THRESHOLD="$threshold"
        fi
        # Critical mi
        local critical
        critical=$(python3 -c "print(1 if $value > ($threshold + $CRITICAL_BAND) else 0)" 2>/dev/null)
        if [ "$critical" = "1" ]; then
            SEVERITY="critical"
        fi
    fi
}

check_metric "cpu"  "$CPU"  "$T_CPU"  "%"
check_metric "mem"  "$MEM"  "$T_MEM"  "%"
check_metric "disk" "$DISK" "$T_DISK" "%"
check_metric "temp" "$TEMP" "$T_TEMP" "C"

if [ $ALERT -eq 1 ]; then
    echo "[$TIMESTAMP] ALERT severity=$SEVERITY primary=$PRIMARY_METRIC ${PRIMARY_VALUE}/${PRIMARY_THRESHOLD} msg=$MSG" >> /var/log/linux-ai-server/alerts.log

    # Lokal ring buffer (debug, izleme)
    curl -s -X POST $API/api/v1/monitor/webhooks/receive \
      -H 'Content-Type: application/json' \
      -d "{\"source\":\"alert-check\",\"event\":\"threshold_exceeded\",\"data\":{\"severity\":\"$SEVERITY\",\"primary_metric\":\"$PRIMARY_METRIC\",\"value\":$PRIMARY_VALUE,\"threshold\":$PRIMARY_THRESHOLD,\"message\":\"$MSG\",\"all\":{\"cpu\":$CPU,\"mem\":$MEM,\"disk\":$DISK,\"temp\":$TEMP},\"timestamp\":\"$TIMESTAMP\"}}" > /dev/null 2>&1

    # Birincil: n8n self-healing webhook
    N8N_PAYLOAD=$(cat <<EOF
{"alert":{"source":"klipper-threshold-${PRIMARY_METRIC}","severity":"$SEVERITY","message":"$(echo "$MSG" | sed 's/"/\\"/g' | tr -d '\n')","value":$PRIMARY_VALUE,"threshold":$PRIMARY_THRESHOLD},"meta":{"hostname":"klipper","timestamp":"$TIMESTAMP","all_metrics":{"cpu":$CPU,"mem":$MEM,"disk":$DISK,"temp":$TEMP}}}
EOF
)
    N8N_HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 -X POST "$N8N_WEBHOOK" \
        -H 'Content-Type: application/json' -d "$N8N_PAYLOAD")

    # Yedek: n8n basarisizsa direkt Telegram (ses kesilmesin)
    if [ "$N8N_HTTP" != "200" ] && [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
        echo "[$TIMESTAMP] N8N_FAILED http=$N8N_HTTP, falling back to direct Telegram" >> /var/log/linux-ai-server/alerts.log
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d chat_id="$TELEGRAM_CHAT_ID" \
            -d parse_mode="Markdown" \
            -d text="⚠️ *Klipper — Eşik Aşıldı (n8n DOWN)*
Severity: ${SEVERITY}
${MSG}
🕐 $(date '+%H:%M %d/%m/%Y')" > /dev/null 2>&1
    fi
else
    echo "[$TIMESTAMP] OK cpu:${CPU}% mem:${MEM}% disk:${DISK}% temp:${TEMP}C" >> /var/log/linux-ai-server/alerts.log
fi
