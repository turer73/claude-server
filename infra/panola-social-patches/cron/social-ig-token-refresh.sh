#!/bin/bash
# Renderhane Instagram Token — Günlük Auto-Refresh
# Cron: 30 7 * * * (her gün 07:30 — social-token-monitor.sh'dan 30dk önce)
# <7 gün kalan token'ı otomatik yeniler; renderhane ve petvet ayrı ayrı.
# Deploy: cp social-ig-token-refresh.sh /opt/linux-ai-server/automation/ && chmod +x ...
# Crontab satırı (automation/crontab'a ekle):
#   30 7 * * * /opt/linux-ai-server/scripts/klipper-cron-wrap.sh social-ig-refresh /opt/linux-ai-server/automation/social-ig-token-refresh.sh
source /opt/linux-ai-server/.env 2>/dev/null

VPS="${VPS_HOST:?Set VPS_HOST in .env}"
SSH="ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 $VPS"
SOCIAL_DIR=/opt/panola-social
PYTHON="${SOCIAL_DIR}/venv/bin/python"
LOG=/var/log/linux-ai-server/social-ig-token-refresh.log
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

send_telegram() {
    local msg="$1"
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="${TELEGRAM_CHAT_ID}" \
        -d parse_mode="Markdown" \
        -d text="$msg" >/dev/null 2>&1
}

check_and_refresh() {
    local account="$1"   # petvet | renderhane
    local product_flag="$2"  # --product petvet | --product renderhane

    RESULT=$($SSH "cd $SOCIAL_DIR && $PYTHON main.py token-check $product_flag 2>&1" 2>&1)

    if echo "$RESULT" | grep -q '"valid".*false\|Token geçersiz\|token.*expired'; then
        echo "[$TS] $account: Token geçersiz — yenileme deneniyor" >> "$LOG"
        REFRESH=$($SSH "cd $SOCIAL_DIR && $PYTHON main.py token-auto $product_flag 2>&1" 2>&1)
        if echo "$REFRESH" | grep -q '"success".*true\|yenilendi\|refreshed'; then
            echo "[$TS] $account: REFRESHED" >> "$LOG"
            send_telegram "🔄 *IG Token Yenilendi — $account*
$account Instagram token'ı yenilendi.
\`$TS\`"
        else
            echo "[$TS] $account: REFRESH FAILED — $REFRESH" >> "$LOG"
            send_telegram "🔴 *IG Token SORUN — $account*
Token geçersiz, yenileme başarısız!
Manuel token gerekiyor.
\`\`\`
$(echo "$REFRESH" | head -c 300)
\`\`\`
\`$TS\`"
        fi
        return
    fi

    # Kalan günü parse et
    DAYS=$(echo "$RESULT" | grep -oP '(?<="days_remaining":)\s*\d+' | tr -d ' ' | head -1)
    DAYS=${DAYS:-$(echo "$RESULT" | grep -oP '\d+(?=\s*gün)' | head -1)}

    if [ -n "$DAYS" ] && [ "$DAYS" -lt 7 ] 2>/dev/null; then
        echo "[$TS] $account: $DAYS gün kaldı — yenileniyor" >> "$LOG"
        REFRESH=$($SSH "cd $SOCIAL_DIR && $PYTHON main.py token-auto $product_flag 2>&1" 2>&1)
        if echo "$REFRESH" | grep -q '"success".*true\|yenilendi\|refreshed'; then
            echo "[$TS] $account: REFRESHED (${DAYS}g kalmıştı)" >> "$LOG"
            send_telegram "🔄 *IG Token Yenilendi — $account*
$account token'ı ${DAYS} gün kala yenilendi.
\`$TS\`"
        else
            echo "[$TS] $account: REFRESH FAILED — $REFRESH" >> "$LOG"
            send_telegram "🔴 *IG Token SORUN — $account*
$account: ${DAYS} gün kalmış, yenileme başarısız!
\`\`\`
$(echo "$REFRESH" | head -c 300)
\`\`\`
\`$TS\`"
        fi
    elif [ -n "$DAYS" ]; then
        echo "[$TS] $account: OK ($DAYS gün kaldı)" >> "$LOG"
    else
        echo "[$TS] $account: OK (gün parse edilemedi) — $RESULT" >> "$LOG"
    fi
}

# PetVet token kontrolü
check_and_refresh "petvet" "--product petvet"

# Renderhane token kontrolü (RENDERHANE_INSTAGRAM_TOKEN .env'de varsa aktif)
if [ -n "${RENDERHANE_INSTAGRAM_TOKEN}" ]; then
    check_and_refresh "renderhane" "--product renderhane"
else
    echo "[$TS] renderhane: SKIP — RENDERHANE_INSTAGRAM_TOKEN .env'de yok" >> "$LOG"
fi

echo "[$TS] Tamamlandı" >> "$LOG"
