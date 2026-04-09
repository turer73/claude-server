#!/bin/bash
# Social Media — Token Monitor & Auto-Refresh
# Cron: 0 8 * * * (her gün 08:00)
# Instagram token geçerliliğini kontrol eder, <7 gün kala otomatik yeniler.
source /opt/linux-ai-server/.env 2>/dev/null

VPS="${VPS_HOST:?VPS_HOST env var required}"
SSH="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 $VPS"
CLI="cd /opt/panola-social && /opt/panola-social/venv/bin/python main.py"
LOG=/var/log/linux-ai-server/social-token.log
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

send_telegram() {
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="$TELEGRAM_CHAT_ID" -d parse_mode="Markdown" -d text="$1" >/dev/null 2>&1
}

# Token auto-refresh (checks + refreshes if < 7 days)
RESULT=$($SSH "$CLI token-auto" 2>&1)

if echo "$RESULT" | grep -q "Token geçerli"; then
    DAYS=$(echo "$RESULT" | grep -oP '\d+ gün')
    echo "[$TS] OK: Token geçerli — $DAYS kaldı" >> "$LOG"
elif echo "$RESULT" | grep -q "success.*true\|yenilendi\|refreshed"; then
    echo "[$TS] REFRESHED: Token yenilendi" >> "$LOG"
    send_telegram "🔄 *Instagram Token Yenilendi*
Token süresi dolmak üzereydi — otomatik yenilendi.
🕐 \`$TS\`"
else
    echo "[$TS] FAILED: $RESULT" >> "$LOG"
    send_telegram "🔴 *Instagram Token SORUN*
Token geçersiz veya yenilenemedi!
Manuel müdahale gerekebilir.
\`\`\`
$(echo "$RESULT" | head -c 300)
\`\`\`"
fi
