#!/bin/bash
# Social Media — Daily Scheduled Publisher
# Cron: 0 9 * * * (her gün 09:00)
# Zamanlanmış içerikleri otomatik yayınlar.
source /opt/linux-ai-server/.env 2>/dev/null

VPS="root@194.163.134.239"
SSH="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 $VPS"
CLI="cd /opt/panola-social && /opt/panola-social/venv/bin/python main.py"
LOG=/var/log/linux-ai-server/social-publish.log
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

send_telegram() {
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="$TELEGRAM_CHAT_ID" -d parse_mode="Markdown" -d text="$1" >/dev/null 2>&1
}

# Publish all content scheduled before now
RESULT=$($SSH "$CLI publish-scheduled" 2>&1)

if echo "$RESULT" | grep -qE '"published":\s*0|"count":\s*0|yayınlanacak içerik yok'; then
    echo "[$TS] SKIP: Yayınlanacak içerik yok" >> "$LOG"
elif echo "$RESULT" | grep -qE '"success":\s*true|"published":'; then
    COUNT=$(echo "$RESULT" | python3 -c '
import sys, json
try:
    data = json.loads(sys.stdin.read())
    print(data.get("published", data.get("count", "?")))
except: print("?")
' 2>/dev/null)
    echo "[$TS] OK: $COUNT post yayınlandı" >> "$LOG"
    send_telegram "📸 *Instagram Post Yayınlandı*
✅ $COUNT içerik otomatik yayınlandı
🕐 \`$TS\`"
else
    echo "[$TS] FAILED: $RESULT" >> "$LOG"
    send_telegram "🔴 *Otomatik Yayın BAŞARISIZ*
\`\`\`
$(echo "$RESULT" | head -c 300)
\`\`\`"
fi

# Collect metrics after publishing
$SSH "$CLI collect-metrics" >/dev/null 2>&1
