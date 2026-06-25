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
STATE_DIR=${RENDERHANE_STATE_DIR:-/opt/linux-ai-server/data/hook-state}  # test izolasyonu için override
STATE_FILE="$STATE_DIR/renderhane-balance-last-alert"
PARTIAL_STREAK_FILE="$STATE_DIR/renderhane-balance-partial-streak"
# Run'lar-arası persistence-gate: izole partial (sonraki saatlik run'da düzelir) page
# ETMEMELİ; yalnız >=GATE ardışık partial = sürekli kesinti → warn/page. In-run retry
# saniye-blip'i, bu gate saat-ölçekli izole kesintiyi filtreler (#205/#207/#209 disiplini).
PARTIAL_GATE=${RENDERHANE_PARTIAL_GATE:-2}

mkdir -p "$STATE_DIR" "$(dirname "$LOG")"

send_telegram() {
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="${TELEGRAM_CHAT_ID}" \
        -d parse_mode="Markdown" \
        -d text="$1" >/dev/null 2>&1
}

# Partial outcome'u run'lar-arası gate'le: ardışık-streak'i artır; <GATE ise page-etme
# (OUTCOME: pass + log), >=GATE ise gerçek partial (warn). Her durumda exit 0.
emit_partial_gated() {
    local detail="$1" streak=0
    # FAIL-OPEN (Codex #219): GATE non-numeric (.env typo) → gate'i 1'e düşür = page et.
    # Sessizce-asla-page-etme'den (config-typo monitoring'i kör eder) güvenli taraf.
    case "$PARTIAL_GATE" in *[!0-9]* | "") echo "[$TS] WARN GATE='$PARTIAL_GATE' geçersiz → fail-open(1)" >> "$LOG" 2>/dev/null; PARTIAL_GATE=1 ;; esac
    [ "$PARTIAL_GATE" -lt 1 ] && PARTIAL_GATE=1
    [ -f "$PARTIAL_STREAK_FILE" ] && streak=$(cat "$PARTIAL_STREAK_FILE" 2>/dev/null || echo 0)
    case "$streak" in *[!0-9]* | "") streak=0 ;; esac
    streak=$((streak + 1))
    # FAIL-OPEN (Codex #219): streak persist edilemezse (hook-state unwritable) gate sayamaz →
    # sürekli kesintide bile asla page etmez (sessiz kör-nokta). Persist-fail → hemen partial/page.
    if ! echo "$streak" > "$PARTIAL_STREAK_FILE" 2>/dev/null; then
        echo "[$TS] WARN streak persist edilemedi → fail-open partial" >> "$LOG" 2>/dev/null
        echo "OUTCOME: partial | $detail (state-persist-fail → fail-open)"
        exit 0
    fi
    if [ "$streak" -ge "$PARTIAL_GATE" ]; then
        echo "OUTCOME: partial | $detail (${streak}. ardışık — sürekli kesinti)"
    else
        echo "[$TS] SUPPRESS izole partial (${streak}/${PARTIAL_GATE} ardışık, blip): $detail" >> "$LOG"
        echo "OUTCOME: pass | transient blip bastırıldı (${streak}/${PARTIAL_GATE}): $detail"
    fi
    exit 0
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
    # Geçici dış-bağımlılık (VPS panola-social erişilemedi) -> run'lar-arası gate'li partial:
    # izole blip page-etmez, ardışık >=GATE sürekli-kesinti warn/page eder.
    emit_partial_gated "VPS /api/health $RETRY_ATTEMPTS-denemede erişilemedi (geçici dış-bağımlılık; saatlik retry)"
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
    # Beklenmedik payload (alan eksik) -> gate'li partial; izole blip page-etmez,
    # ardışık >=GATE kalıcı payload-değişimi warn/page eder.
    emit_partial_gated "renderhane_balance alanı yanıtta yok (payload değişmiş olabilir)"
fi

# Balance başarıyla okundu → ardışık-partial streak'i sıfırla (kesinti bitti).
rm -f "$PARTIAL_STREAK_FILE"
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
