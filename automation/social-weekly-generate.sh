#!/bin/bash
# Social Media — Weekly Content Generation
# Cron: 0 10 * * 0 (her Pazar 10:00)
# Haftalık içerik planı + postları otomatik üretir.
source /opt/linux-ai-server/.env 2>/dev/null

VPS="root@REDACTED_VPS_IP"
SSH="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 $VPS"
CLI="cd /opt/panola-social && /opt/panola-social/venv/bin/python main.py"
LOG=/var/log/linux-ai-server/social-generate.log
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

send_telegram() {
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="$TELEGRAM_CHAT_ID" -d parse_mode="Markdown" -d text="$1" >/dev/null 2>&1
}

# Generate full week content (plan + posts + images)
RESULT=$($SSH "$CLI generate-week --product petvet" 2>&1)

if echo "$RESULT" | grep -qE '"success":\s*true|"generated":|"posts":'; then
    COUNT=$(echo "$RESULT" | python3 -c '
import sys, json
try:
    data = json.loads(sys.stdin.read())
    print(data.get("generated", data.get("count", "?")))
except: print("?")
' 2>/dev/null)
    echo "[$TS] OK: $COUNT post üretildi" >> "$LOG"
    send_telegram "✅ *Haftalık İçerik Üretildi*
📝 $COUNT yeni post oluşturuldu (petvet)
📅 Bu hafta için plan hazır
🕐 \`$TS\`

Onay için: \`/api/v1/social/content/list?status=draft\`"
else
    echo "[$TS] FAILED: $RESULT" >> "$LOG"
    send_telegram "🔴 *Haftalık İçerik Üretimi BAŞARISIZ*
\`\`\`
$(echo "$RESULT" | head -c 300)
\`\`\`"
fi
