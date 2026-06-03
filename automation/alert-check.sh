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
N8N_WEBHOOK="http://localhost:5678/webhook/klipper-alert"

# .env'den oku
if [ -f /opt/linux-ai-server/.env ]; then
  set -a; source /opt/linux-ai-server/.env; set +a
fi

# GÜVENLIK: /metrics artık auth ŞART (auth-bypass fix). Internal automation ->
# X-API-Key (=INTERNAL_API_KEY, .env'den source edildi) -> admin scope.
RESP=$(curl -s --max-time 10 -w "\n%{http_code}" -H "X-API-Key: ${INTERNAL_API_KEY}" $API/api/v1/monitor/metrics)
HTTP_CODE=$(printf '%s' "$RESP" | tail -n1)
METRICS=$(printf '%s' "$RESP" | sed '$d')
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# FAIL-CLOSED (Codex #27 P2): /metrics auth/fetch başarısızsa (key eksik/rotate, API
# down) SESSIZ 0-parse DEĞİL -> 0-default'lar hiçbir eşik tetiklemez = alert-pipeline
# körleşir (planın tezi: sessiz-arıza). Explicit critical alert + abort.
if [ "$HTTP_CODE" != "200" ]; then
    FAILMSG="monitoring /metrics auth/fetch FAIL http=${HTTP_CODE} (INTERNAL_API_KEY eksik/rotate? API down?) — alert-pipeline KÖR"
    BODY="{\"alert\":{\"source\":\"alert-check\",\"severity\":\"critical\",\"message\":\"${FAILMSG}\",\"value\":0,\"threshold\":0},\"meta\":{\"type\":\"monitoring_self_failure\",\"http\":\"${HTTP_CODE}\",\"device\":\"klipper\"}}"
    curl -s -X POST --max-time 5 -H "Content-Type: application/json" \
        -H "X-Webhook-Secret: ${WEBHOOK_SECRET:-MISSING}" -d "$BODY" "$N8N_WEBHOOK" >/dev/null 2>&1 || true
    echo "OUTCOME: fail | ${FAILMSG}"
    exit 1
fi

CPU=$(echo $METRICS | python3 -c 'import sys,json; print(json.load(sys.stdin).get("cpu_percent",0))')
MEM=$(echo $METRICS | python3 -c 'import sys,json; print(json.load(sys.stdin).get("memory_percent",0))')
DISK=$(echo $METRICS | python3 -c 'import sys,json; print(json.load(sys.stdin).get("disk_percent",0))')
TEMP=$(echo $METRICS | python3 -c 'import sys,json; print(json.load(sys.stdin).get("temperature",0))')

# Esikler
# 2026-05-27: Ryzen 7 8C/16T gece cron'lari (otonom hook + test-runner + demo-reset)
# 85 esigi sik tetikliyordu (24h: 11 warning alarm, hepsi 85-95% arasi spike).
# Sustained-N pattern (ileri is) eklenene kadar threshold 90'a cikarildi.
T_CPU=90
T_MEM=88
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

# Sustained-N pattern (2026-05-27): state file ile son N olcumu tutariz.
# Cron 5dk freq + N=3 → 15dk sustained yakalar. Anlik spike yutulur (cron-heavy gece).
STATE_DIR="/var/lib/linux-ai-server/alert-state"
SUSTAINED_N=3
mkdir -p "$STATE_DIR" 2>/dev/null

# Append value, keep last N (atomic write via tmp+mv)
record_metric() {
    local metric="$1" value="$2"
    local file="$STATE_DIR/${metric}.history"
    {
        [ -f "$file" ] && cat "$file"
        echo "$value"
    } | tail -n "$SUSTAINED_N" > "$file.tmp" 2>/dev/null && mv "$file.tmp" "$file" 2>/dev/null
}

# Return 0 if ALL last-N values are above threshold (sustained breach)
is_sustained_breach() {
    local metric="$1" threshold="$2"
    local file="$STATE_DIR/${metric}.history"
    [ ! -f "$file" ] && return 1
    local total breached
    total=$(wc -l < "$file" 2>/dev/null)
    breached=$(awk -v t="$threshold" '$1 > t' "$file" 2>/dev/null | wc -l)
    [ "$total" -eq "$SUSTAINED_N" ] && [ "$breached" -eq "$SUSTAINED_N" ]
}

check_metric() {
    local name="$1" value="$2" threshold="$3" suffix="$4"

    # Record value into rolling history (sustained-N evaluation)
    record_metric "$name" "$value"

    # Anlik breach kontrol (debug log icin)
    local breached
    breached=$(python3 -c "print(1 if $value > $threshold else 0)" 2>/dev/null)
    if [ "$breached" != "1" ]; then
        return
    fi

    # Anlik esik asti ama 3 ardisikta esiklemediyse yutuyoruz (spike)
    # Sustained sart: son N olcum tumu esik ustunde.
    if ! is_sustained_breach "$name" "$threshold"; then
        # Spike yutuldu — sadece log'a not (debug), Telegram'a gitmez
        echo "[$TIMESTAMP] SPIKE_SUPPRESSED ${name}=${value}${suffix} (>${threshold}, sustained-${SUSTAINED_N} hentuz dolmadi)" >> /var/log/linux-ai-server/alerts.log
        return
    fi

    # 3+ ardisik esik asti → gercek alarm
    MSG="$MSG ${name}:${value}${suffix}"
    ALERT=1
    local highest
    highest=$(python3 -c "print(1 if $value > $PRIMARY_VALUE else 0)" 2>/dev/null)
    if [ "$highest" = "1" ]; then
        PRIMARY_METRIC="$name"
        PRIMARY_VALUE="$value"
        PRIMARY_THRESHOLD="$threshold"
    fi
    # Critical mi (esik + 10pp ustu sustained → kritik)
    local critical
    critical=$(python3 -c "print(1 if $value > ($threshold + $CRITICAL_BAND) else 0)" 2>/dev/null)
    if [ "$critical" = "1" ]; then
        SEVERITY="critical"
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
      -H "X-API-Key: ${INTERNAL_API_KEY:-MISSING}" \
      -d "{\"source\":\"alert-check\",\"event\":\"threshold_exceeded\",\"data\":{\"severity\":\"$SEVERITY\",\"primary_metric\":\"$PRIMARY_METRIC\",\"value\":$PRIMARY_VALUE,\"threshold\":$PRIMARY_THRESHOLD,\"message\":\"$MSG\",\"all\":{\"cpu\":$CPU,\"mem\":$MEM,\"disk\":$DISK,\"temp\":$TEMP},\"timestamp\":\"$TIMESTAMP\"}}" > /dev/null 2>&1

    # Birincil: n8n self-healing webhook
    N8N_PAYLOAD=$(cat <<EOF
{"alert":{"source":"klipper-threshold-${PRIMARY_METRIC}","severity":"$SEVERITY","message":"$(echo "$MSG" | sed 's/"/\\"/g' | tr -d '\n')","value":$PRIMARY_VALUE,"threshold":$PRIMARY_THRESHOLD},"meta":{"hostname":"klipper","timestamp":"$TIMESTAMP","all_metrics":{"cpu":$CPU,"mem":$MEM,"disk":$DISK,"temp":$TEMP}}}
EOF
)
    N8N_HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 -X POST "$N8N_WEBHOOK" \
        -H 'Content-Type: application/json' \
        -H "X-Webhook-Secret: ${WEBHOOK_SECRET:-MISSING}" \
        -d "$N8N_PAYLOAD")

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
