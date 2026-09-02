#!/bin/bash
# Daily automated backup with Telegram notification
source /opt/linux-ai-server/.env 2>/dev/null

API=http://localhost:8420
KEY="${API_KEY:?Set API_KEY in .env}"
LOG=/var/log/linux-ai-server/backup.log

send_telegram() {
    curl --max-time 15 --connect-timeout 5 -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="$TELEGRAM_CHAT_ID" -d parse_mode="Markdown" -d text="$1" >/dev/null 2>&1
}

# Auth
TOKEN=$(curl -s -X POST $API/api/v1/auth/token \
    -H 'Content-Type: application/json' \
    -d "{\"api_key\": \"$KEY\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null)

if [ -z "$TOKEN" ]; then
    # Auth dusunce kosulsuz "servis calismiyor olabilir" demek YANLIS TESHIS
    # uretiyordu: 2026-08-31/09-02'de gercek neden BOZUK server.db idi (auth
    # api_keys'i oradan okur) ama alarm servisi isaret etti ve 45 saat yanlis
    # yone baktirdi. Artik iki degisken AYRI olculur: servis ayakta mi, DB saglam mi.
    TS_NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    HEALTH=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$API/health" 2>/dev/null)
    DB_FILE="${SERVER_DB:-/opt/linux-ai-server/data/server.db}"
    DB_STATE="bilinmiyor"
    if [ -f "$DB_FILE" ] && command -v sqlite3 >/dev/null 2>&1; then
        if [ "$(sqlite3 "$DB_FILE" 'PRAGMA quick_check;' 2>&1 | head -1)" = "ok" ]; then
            DB_STATE="saglam"
        else
            DB_STATE="BOZUK"
        fi
    fi

    if [ "$HEALTH" != "200" ]; then
        REASON="servis yanit vermiyor (/health=${HEALTH:-yok})"
    elif [ "$DB_STATE" = "BOZUK" ]; then
        REASON="servis AYAKTA ama server.db BOZUK - auth api_keys'i okuyamiyor"
    else
        REASON="servis ayakta (/health=200), server.db ${DB_STATE} - auth anahtar sorunu (API_KEY gecersiz/rotate edilmis?)"
    fi

    echo "[$TS_NOW] AUTH FAILED: $REASON" >> "$LOG"
    send_telegram "🔴 *Backup FAILED*
API auth basarisiz.
Neden: $REASON"
    echo "OUTCOME: fail | API auth basarisiz - $REASON"
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
    echo "OUTCOME: pass | $FILENAME $SIZE kept=$KEPT"
else
    echo "[$TIMESTAMP] FAILED: $RESULT" >> "$LOG"
    send_telegram "🔴 *Backup FAILED*
Label: $LABEL
Hata: \`$(echo $RESULT | head -c 200)\`"
    echo "OUTCOME: fail | $(echo "$RESULT" | tr -d '\n' | head -c 120)"
    exit 1
fi
