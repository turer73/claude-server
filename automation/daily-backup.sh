#!/bin/bash
# Daily automated backup with Telegram notification
source /opt/linux-ai-server/.env 2>/dev/null

API=http://localhost:8420
KEY="${API_KEY:?Set API_KEY in .env}"
LOG=/var/log/linux-ai-server/backup.log

send_telegram() {
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="$TELEGRAM_CHAT_ID" -d parse_mode="Markdown" -d text="$1" >/dev/null 2>&1
}

# Auth
TOKEN=$(curl -s -X POST $API/api/v1/auth/token \
    -H 'Content-Type: application/json' \
    -d "{\"api_key\": \"$KEY\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null)

if [ -z "$TOKEN" ]; then
    MSG="🔴 *Backup FAILED*\nAPI auth başarısız — servis çalışmıyor olabilir"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] AUTH FAILED" >> "$LOG"
    send_telegram "$MSG"
    exit 1
fi

# Create backup
LABEL="auto-$(date +%Y%m%d-%H%M)"
RESULT=$(curl -s -X POST "$API/api/v1/backup/create?label=$LABEL" \
    -H "Authorization: Bearer $TOKEN")

SUCCESS=$(echo "$RESULT" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("success",False))' 2>/dev/null)
FILENAME=$(echo "$RESULT" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("filename","?"))' 2>/dev/null)
SIZE=$(echo "$RESULT" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(f"{d.get(\"size_bytes\",0)/1024/1024:.1f}MB")' 2>/dev/null)

TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

if [ "$SUCCESS" = "True" ]; then
    echo "[$TIMESTAMP] OK: $FILENAME ($SIZE)" >> "$LOG"
    # Cleanup: keep only last 7 backups
    ls -t /var/lib/linux-ai-server/backups/*.tar.gz 2>/dev/null | tail -n +8 | xargs rm -f 2>/dev/null
    KEPT=$(ls /var/lib/linux-ai-server/backups/*.tar.gz 2>/dev/null | wc -l)
    DISK=$(df -h / | awk 'NR==2{print $4}')
    send_telegram "✅ *Backup OK*
📦 \`$FILENAME\`
💾 Boyut: $SIZE | Toplam: ${KEPT} yedek
🖥 Kalan disk: $DISK"
else
    echo "[$TIMESTAMP] FAILED: $RESULT" >> "$LOG"
    send_telegram "🔴 *Backup FAILED*
Label: $LABEL
Hata: \`$(echo $RESULT | head -c 200)\`"
    exit 1
fi
