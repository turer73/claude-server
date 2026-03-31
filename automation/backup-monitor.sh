#!/bin/bash
# Backup Monitor — yedeklerin düzenli alındığını ve sağlıklı olduğunu kontrol eder
# Systemd timer ile günde 2x çalışır (09:00, 18:00)
source /opt/linux-ai-server/.env 2>/dev/null

BACKUP_DIR="/var/lib/linux-ai-server/backups"
MAX_AGE_HOURS=28  # 28 saatten eski = backup alınmamış
LOG_TAG="backup-monitor"

send_telegram() {
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="$TELEGRAM_CHAT_ID" -d parse_mode="Markdown" -d text="$1" >/dev/null 2>&1
}

# 1. Backup dizini var mı?
if [ ! -d "$BACKUP_DIR" ]; then
    logger -t "$LOG_TAG" "Backup directory missing: $BACKUP_DIR"
    send_telegram "🔴 *Backup Monitor*
Backup dizini bulunamadı: \`$BACKUP_DIR\`"
    exit 1
fi

# 2. Hiç backup var mı?
LATEST=$(ls -t "$BACKUP_DIR"/*.tar.gz 2>/dev/null | head -1)
if [ -z "$LATEST" ]; then
    logger -t "$LOG_TAG" "No backups found"
    send_telegram "🔴 *Backup Monitor*
Hiç yedek dosyası bulunamadı!"
    exit 1
fi

# 3. Son backup ne zaman alınmış?
LATEST_EPOCH=$(stat -c %Y "$LATEST")
NOW_EPOCH=$(date +%s)
AGE_HOURS=$(( (NOW_EPOCH - LATEST_EPOCH) / 3600 ))

if [ "$AGE_HOURS" -ge "$MAX_AGE_HOURS" ]; then
    LATEST_NAME=$(basename "$LATEST")
    logger -t "$LOG_TAG" "Backup stale: $LATEST_NAME is ${AGE_HOURS}h old"
    send_telegram "🟡 *Backup Monitor — Uyarı*
Son yedek *${AGE_HOURS} saat* önce alınmış!
📦 \`$LATEST_NAME\`
Günlük backup çalışmamış olabilir."
    exit 1
fi

# 4. Son backup bozuk mu? (tar integrity check)
LATEST_NAME=$(basename "$LATEST")
LATEST_SIZE=$(stat -c %s "$LATEST")

if [ "$LATEST_SIZE" -lt 1024 ]; then
    logger -t "$LOG_TAG" "Backup too small: $LATEST_NAME (${LATEST_SIZE}B)"
    send_telegram "🔴 *Backup Monitor*
Son yedek çok küçük (${LATEST_SIZE}B), bozuk olabilir!
📦 \`$LATEST_NAME\`"
    exit 1
fi

if ! tar -tzf "$LATEST" >/dev/null 2>&1; then
    logger -t "$LOG_TAG" "Backup corrupt: $LATEST_NAME"
    send_telegram "🔴 *Backup Monitor*
Son yedek bozuk — açılamıyor!
📦 \`$LATEST_NAME\`"
    exit 1
fi

# 5. Her şey OK
BACKUP_COUNT=$(ls "$BACKUP_DIR"/*.tar.gz 2>/dev/null | wc -l)
SIZE_MB=$(echo "scale=1; $LATEST_SIZE / 1048576" | bc)
logger -t "$LOG_TAG" "OK: $LATEST_NAME (${SIZE_MB}MB, ${AGE_HOURS}h ago, $BACKUP_COUNT total)"
