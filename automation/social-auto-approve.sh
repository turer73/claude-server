#!/bin/bash
# Social Media — Auto Approve & Schedule Drafts
# Cron: 0 11 * * 0 (her Pazar 11:00, generate sonrası)
# Üretilen draft içerikleri onaylar ve haftanın günlerine zamanlar.
source /opt/linux-ai-server/.env 2>/dev/null

VPS="root@194.163.134.239"
SSH="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 $VPS"
SOCIAL_DIR="/opt/panola-social"
PYTHON="$SOCIAL_DIR/venv/bin/python"
LOG=/var/log/linux-ai-server/social-approve.log
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

send_telegram() {
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="$TELEGRAM_CHAT_ID" -d parse_mode="Markdown" -d text="$1" >/dev/null 2>&1
}

# Approve all drafts and schedule them across the week (Mon-Sat 09:00)
RESULT=$($SSH "cd $SOCIAL_DIR && $PYTHON -c \"
import json
from datetime import datetime, timedelta
from src.db import list_contents, update_content_status

drafts = list_contents(status='draft', limit=50)
if not drafts:
    print(json.dumps({'approved': 0, 'message': 'No drafts'}))
else:
    approved = 0
    # Schedule across next week days (Mon=0 to Sat=5)
    base = datetime.now()
    # Find next Monday
    days_ahead = 7 - base.weekday() if base.weekday() > 0 else 7
    monday = base + timedelta(days=days_ahead)

    for i, draft in enumerate(drafts):
        day_offset = i % 6  # Mon-Sat
        schedule_dt = (monday + timedelta(days=day_offset)).replace(hour=9, minute=0, second=0)
        update_content_status(draft['id'], 'approved')
        update_content_status(draft['id'], 'scheduled', scheduled_at=schedule_dt.isoformat())
        approved += 1

    print(json.dumps({
        'approved': approved,
        'schedule_start': monday.strftime('%Y-%m-%d'),
        'schedule_end': (monday + timedelta(days=5)).strftime('%Y-%m-%d'),
    }))
\"" 2>&1)

if echo "$RESULT" | grep -qE '"approved":\s*0'; then
    echo "[$TS] SKIP: Onaylanacak draft yok" >> "$LOG"
elif echo "$RESULT" | grep -qE '"approved":'; then
    COUNT=$(echo "$RESULT" | python3 -c '
import sys, json
try:
    data = json.loads(sys.stdin.read())
    print(f"{data[\"approved\"]} post, {data.get(\"schedule_start\",\"?\")} — {data.get(\"schedule_end\",\"?\")}")
except: print("?")
' 2>/dev/null)
    echo "[$TS] OK: $COUNT" >> "$LOG"
    send_telegram "✅ *İçerikler Onaylandı & Zamanlandı*
📋 $COUNT
🕐 \`$TS\`

Pazartesi'den itibaren günlük 09:00'da yayınlanacak."
else
    echo "[$TS] FAILED: $RESULT" >> "$LOG"
    send_telegram "🔴 *Otomatik Onay BAŞARISIZ*
\`\`\`
$(echo "$RESULT" | head -c 300)
\`\`\`"
fi
