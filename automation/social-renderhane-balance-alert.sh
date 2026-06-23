#!/bin/bash
# Renderhane bakiye uyarisi (panola-social VPS /api/health proxy).
# v2-06 PSOC-20260528 (surer #99575). Cron: 0 * * * * (saatlik).
# Bakiye threshold altina duserse Telegram alert; cooldown spam'i onler.
#
# .env gereksinimi:
#   RENDERHANE_BALANCE_THRESHOLD=200    (default 200 olur)
#   RENDERHANE_BALANCE_COOLDOWN=6       (saat, default 6)
#   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (zaten mevcut)
#
# Cron entry (automation/crontab):
#   0 * * * * /opt/linux-ai-server/scripts/klipper-cron-wrap.sh renderhane-balance \
#     /opt/linux-ai-server/automation/social-renderhane-balance-alert.sh

source /opt/linux-ai-server/.env 2>/dev/null

LOG=/var/log/linux-ai-server/social-renderhane-balance.log
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
URL="http://100.126.113.23:9800/api/health"   # VPS panola-social Tailscale node
THRESHOLD=${RENDERHANE_BALANCE_THRESHOLD:-200}
COOLDOWN_HOURS=${RENDERHANE_BALANCE_COOLDOWN:-6}
STATE_DIR=/opt/linux-ai-server/data/hook-state
STATE_FILE="$STATE_DIR/renderhane-balance-last-alert"

mkdir -p "$STATE_DIR" "$(dirname "$LOG")"

send_telegram() {
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="${TELEGRAM_CHAT_ID}" \
        -d parse_mode="Markdown" \
        -d text="$1" >/dev/null 2>&1
}

# In-run retry: geçici Tailscale/VPS blip'i sahte partial-page'e çevirme (klipper 2026-06-23;
# #205/#207/#209 ile aynı "tek-örnek != sürekli-sorun" disiplini — partial'ların çoğu blip,
# balance erişildiğinde sağlıklı). N deneme + kısa backoff; ancak HEPSİ boş kalırsa partial.
# Deneme/backoff env ile ayarlanır (test hızı: RENDERHANE_RETRY_SLEEP=0).
RETRY_ATTEMPTS=${RENDERHANE_RETRY_ATTEMPTS:-3}
RETRY_SLEEP=${RENDERHANE_RETRY_SLEEP:-3}
RESPONSE=""
for _att in $(seq 1 "$RETRY_ATTEMPTS"); do
    RESPONSE=$(curl -s --max-time 10 "$URL" 2>/dev/null)
    [ -n "$RESPONSE" ] && break
    [ "$_att" -lt "$RETRY_ATTEMPTS" ] && sleep "$RETRY_SLEEP"
done
if [ -z "$RESPONSE" ]; then
    echo "[$TS] ERROR: VPS /api/health timeout/empty ($RETRY_ATTEMPTS deneme)" >> "$LOG"
    # Geçici dış-bağımlılık (VPS panola-social erişilemedi) -> partial (warning),
    # CRITICAL DEĞİL: bir sonraki saatlik run'da düzelir. (outcome-contract)
    echo "OUTCOME: partial | VPS /api/health $RETRY_ATTEMPTS-denemede erişilemedi (geçici dış-bağımlılık; saatlik retry)"
    exit 0
fi

BALANCE=$(echo "$RESPONSE" | python3 -c '
import sys, json
try:
    d = json.loads(sys.stdin.read())
    print(d.get("renderhane_balance", -1))
except Exception:
    print(-1)
' 2>/dev/null)

if [ -z "$BALANCE" ] || [ "$BALANCE" = "-1" ]; then
    echo "[$TS] PARSE ERROR: renderhane_balance missing in: $RESPONSE" >> "$LOG"
    # Beklenmedik payload (alan eksik) -> partial (warning), CRITICAL değil; transient
    # olabilir, kalıcıysa warning olarak yüzeyde kalır (outcome-contract).
    echo "OUTCOME: partial | renderhane_balance alanı yanıtta yok (payload değişmiş olabilir)"
    exit 0
fi

echo "[$TS] balance=$BALANCE threshold=$THRESHOLD" >> "$LOG"

if [ "$BALANCE" -lt "$THRESHOLD" ]; then
    NOW=$(date +%s)
    LAST=0
    [ -f "$STATE_FILE" ] && LAST=$(cat "$STATE_FILE")
    COOLDOWN=$((COOLDOWN_HOURS * 3600))
    if [ $((NOW - LAST)) -ge $COOLDOWN ]; then
        send_telegram "💸 *Renderhane Bakiye Düşük*
Panola-social Renderhane bakiyesi: \`$BALANCE\` kredi
Eşik: \`$THRESHOLD\`
Önlem: Renderhane hesabına kredi ekleyin
\`$TS\`"
        echo "$NOW" > "$STATE_FILE"
        echo "[$TS] ALERT SENT (balance $BALANCE < $THRESHOLD)" >> "$LOG"
    else
        REMAIN=$(( (COOLDOWN - (NOW - LAST)) / 60 ))
        echo "[$TS] SKIP: cooldown active (${REMAIN}m kaldi)" >> "$LOG"
    fi
fi

# Bakiye okundu (alarm ayrı Telegram yoluyla zaten gönderildi); script sağlıklı çalıştı.
# Düşük-bakiye = iş-durumu (kendi alarmı var), script-OUTCOME'u değil -> pass.
echo "OUTCOME: pass | balance=${BALANCE} threshold=${THRESHOLD}"
