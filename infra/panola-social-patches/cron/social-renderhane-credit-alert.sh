#!/bin/bash
# Renderhane — Anthropic API Kredi Kullanım Uyarısı
# Cron: 0 * * * * (saatlik)
# Anthropic API aylık kullanımını izler. Tahmini harcama $THRESHOLD üstüne çıkarsa Telegram alert.
# Deploy: cp social-renderhane-credit-alert.sh /opt/linux-ai-server/automation/ && chmod +x ...
# Crontab satırı (automation/crontab'a ekle):
#   0 * * * * /opt/linux-ai-server/scripts/klipper-cron-wrap.sh social-renderhane-credit /opt/linux-ai-server/automation/social-renderhane-credit-alert.sh
source /opt/linux-ai-server/.env 2>/dev/null

LOG=/var/log/linux-ai-server/social-renderhane-credit.log
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
THRESHOLD=${RENDERHANE_CREDIT_THRESHOLD:-200}
STATE_DIR=/opt/linux-ai-server/data/hook-state
STATE_FILE=$STATE_DIR/renderhane-credit-alerted

mkdir -p "$STATE_DIR"

send_telegram() {
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="${TELEGRAM_CHAT_ID}" \
        -d parse_mode="Markdown" \
        -d text="$1" >/dev/null 2>&1
}

# Anthropic API connectivity check + 529 overload tespiti
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    --max-time 10 \
    -H "x-api-key: ${ANTHROPIC_API_KEY}" \
    -H "anthropic-version: 2023-06-01" \
    "https://api.anthropic.com/v1/models" 2>/dev/null)

if [ "$HTTP_CODE" = "529" ]; then
    echo "[$TS] OVERLOADED: Anthropic API 529 — yüksek yük veya kota tükenmesi" >> "$LOG"

    # Son 1 saatte 3+ kez 529 geldiyse alert
    RECENT=$(grep -c "OVERLOADED" "$LOG" 2>/dev/null | tail -1)
    HOUR_COUNT=$(grep "OVERLOADED" "$LOG" 2>/dev/null | grep "$(date -u +%Y-%m-%dT%H)" | wc -l)

    if [ "${HOUR_COUNT:-0}" -ge 3 ]; then
        TODAY=$(date -u +%Y-%m-%d)
        if ! grep -q "$TODAY-529" "$STATE_FILE" 2>/dev/null; then
            echo "$TODAY-529" >> "$STATE_FILE"
            send_telegram "⚠️ *Renderhane Anthropic API — 529 Tekrarlı*
Son 1 saatte ${HOUR_COUNT}x 529 (overloaded) hatası.
Kredi tükenmesi veya kota aşımı olabilir.
Kontrol: console.anthropic.com
\`$TS\`"
        fi
    fi
    exit 0

elif [ "$HTTP_CODE" = "401" ] || [ "$HTTP_CODE" = "403" ]; then
    echo "[$TS] AUTH FAIL: HTTP $HTTP_CODE — ANTHROPIC_API_KEY geçersiz?" >> "$LOG"
    TODAY=$(date -u +%Y-%m-%d)
    if ! grep -q "$TODAY-auth" "$STATE_FILE" 2>/dev/null; then
        echo "$TODAY-auth" >> "$STATE_FILE"
        send_telegram "🔴 *Renderhane Anthropic API Auth Hatası*
HTTP $HTTP_CODE — API key geçersiz veya iptal edilmiş!
Kontrol: console.anthropic.com
\`$TS\`"
    fi
    exit 1
fi

# Anthropic workspace kullanım API'si (beta endpoint)
USAGE_JSON=$(curl -s -f --max-time 10 \
    -H "anthropic-version: 2023-06-01" \
    -H "x-api-key: ${ANTHROPIC_API_KEY}" \
    -H "anthropic-beta: usage-2025-03-01" \
    "https://api.anthropic.com/v1/usage?period=current_month" 2>/dev/null)

if [ -z "$USAGE_JSON" ] || echo "$USAGE_JSON" | grep -q '"error"'; then
    echo "[$TS] OK: API erişilebilir (HTTP $HTTP_CODE), kullanım API yok — sadece connectivity doğrulandı" >> "$LOG"
    exit 0
fi

# Aylık harcamayı parse et
COST=$(echo "$USAGE_JSON" | python3 -c '
import sys, json
try:
    d = json.loads(sys.stdin.read())
    cost = (d.get("total_cost_usd") or
            d.get("cost") or
            (d.get("data") or {}).get("total_cost_usd") or 0)
    print(f"{float(cost):.2f}")
except:
    print("0")
' 2>/dev/null)

echo "[$TS] Aylık kullanım: \$$COST (limit: \$$THRESHOLD)" >> "$LOG"

EXCEEDED=$(python3 -c "print('yes' if float('${COST:-0}') >= ${THRESHOLD} else 'no')" 2>/dev/null)

if [ "$EXCEEDED" = "yes" ]; then
    TODAY=$(date -u +%Y-%m-%d)
    if grep -q "$TODAY-cost" "$STATE_FILE" 2>/dev/null; then
        echo "[$TS] SKIP: Bugün zaten cost alert gönderildi" >> "$LOG"
        exit 0
    fi
    echo "$TODAY-cost" >> "$STATE_FILE"
    echo "[$TS] ALERT: \$$COST >= \$$THRESHOLD" >> "$LOG"
    send_telegram "💸 *Renderhane Anthropic Kredi Uyarısı*
Aylık harcama: \`\$$COST\`
Eşik: \`\$$THRESHOLD\`

Kontrol: console.anthropic.com
\`$TS\`"
else
    # Başarılı kontrol — state dosyasından eski cost satırlarını temizle (yeni ay başı)
    grep -v "cost" "$STATE_FILE" 2>/dev/null > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "$STATE_FILE" || true
fi
