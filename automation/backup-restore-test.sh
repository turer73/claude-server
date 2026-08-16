#!/bin/bash
# backup-restore-test.sh — En yeni backup'i gecici dizine ac, SQLite integrity dogrula.
#
# Cron: 20 3 * * * (gunluk 03:20, daily-backup sonrasi) — klipper-cron-wrap ile sarili
# Telegram: SADECE fail durumunda (PASS sessiz). OUTCOME marker -> cron_outcomes (wrap).
# Exit: 0 OK, 1 fail
#
# 2026-05-27 ekleme — "yedek alindi" != "yedek calisir". Restore-time validation.

set -uo pipefail
source /opt/linux-ai-server/.env 2>/dev/null

# Yollar override edilebilir — TEK sebebi test edilebilirlik: pytest bu script'i
# sahte bir backup dizini uzerinde kosturup davranisini dogruluyor
# (tests/test_backup_restore_test_sh.py). Cron ortaminda degisken tanimli
# olmadigi icin uretim yollari aynen gecerli.
BACKUP_DIR="${BACKUP_DIR:-/var/lib/linux-ai-server/backups}"
LOG="${RESTORE_TEST_LOG:-/var/log/linux-ai-server/backup-restore-test.log}"
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

mkdir -p "$(dirname "$LOG")" 2>/dev/null

log() { echo "[$TS] $*" | tee -a "$LOG"; }

send_telegram() {
    [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ] && return
    curl --max-time 15 --connect-timeout 5 -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="$TELEGRAM_CHAT_ID" -d parse_mode="Markdown" \
        -d text="$1" > /dev/null 2>&1
}

# 1) En yeni backup'i bul
LATEST=$(ls -t "$BACKUP_DIR"/*.tar.gz 2>/dev/null | head -1)
if [ -z "$LATEST" ]; then
    log "FAIL: backup bulunamadi"
    echo "OUTCOME: fail | backup bulunamadi: $BACKUP_DIR"
    send_telegram "🔴 *Backup Restore Test*
Backup bulunamadi: \`$BACKUP_DIR\`"
    exit 1
fi
LATEST_NAME=$(basename "$LATEST")
log "Test ediliyor: $LATEST_NAME"

# 2) Gecici dizine ac
TMP=$(mktemp -d -t restore-test-XXXXXX)
trap "rm -rf '$TMP'" EXIT

if ! tar -xzf "$LATEST" -C "$TMP" 2>>"$LOG"; then
    log "FAIL: tar acilmadi"
    echo "OUTCOME: fail | tar açılamadı (corrupt?): $LATEST_NAME"
    send_telegram "🔴 *Backup Restore Test FAIL*
\`$LATEST_NAME\` tar.gz acilamadi (corrupt?)"
    exit 1
fi

# 3) Tum .db dosyalarini bulup integrity_check
DB_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0
FAIL_NAMES=""
while IFS= read -r -d '' db; do
    # ADI ".db" olan her sey SQLite DEGIL. data/hook-state/ altindaki playbook
    # cooldown damgalari hedef-adiyla isimlendiriliyor ve bu deseni tutturuyor
    # (ornek: investigate-db-integrity_server.db = 18 baytlik unix timestamp).
    # 2026-08-15'te testi kalici kirmiziya cekti -> alarm korlugu riski.
    # Ada degil ICERIGE bak: SQLite dosyasi "SQLite format 3\0" ile baslar.
    # NUL-guvenli: $(...) NUL bayti iceren ikili dosyada bash uyarisi basardi
    # ("ignored null byte in input"). Ayni kontrol backup-docker-volumes.sh'te de var.
    if ! head -c 15 "$db" 2>/dev/null | cmp -s - <(printf 'SQLite format 3'); then
        SKIP_COUNT=$((SKIP_COUNT + 1))
        log "  – $(basename "$db") atlandi (SQLite basligi yok, DB degil)"
        continue
    fi
    DB_COUNT=$((DB_COUNT + 1))
    result=$(sqlite3 "$db" "PRAGMA integrity_check;" 2>&1)
    if [ "$result" = "ok" ]; then
        log "  ✓ $(basename "$db") OK"
    else
        FAIL_COUNT=$((FAIL_COUNT + 1))
        FAIL_NAMES="${FAIL_NAMES}$(basename "$db"): ${result:0:50}\n"
        log "  ✗ $(basename "$db") FAIL: ${result:0:100}"
    fi
done < <(find "$TMP" -name "*.db" -type f -print0)

if [ "$DB_COUNT" -eq 0 ]; then
    log "FAIL: backup'ta hic .db dosyasi yok"
    echo "OUTCOME: fail | $LATEST_NAME içinde SQLite DB yok"
    send_telegram "🔴 *Backup Restore Test FAIL*
\`$LATEST_NAME\` icinde hic SQLite DB yok!"
    exit 1
fi

# 4) Sonuc
if [ "$FAIL_COUNT" -gt 0 ]; then
    log "FAIL: $FAIL_COUNT/$DB_COUNT DB bozuk"
    echo "OUTCOME: fail | $FAIL_COUNT/$DB_COUNT DB bozuk ($LATEST_NAME)"
    send_telegram "🔴 *Backup Restore Test FAIL*
\`$LATEST_NAME\` ($DB_COUNT DB, $FAIL_COUNT bozuk)

Bozuk:
$FAIL_NAMES"
    exit 1
fi

log "PASS: $DB_COUNT DB hepsi integrity OK (atlanan: $SKIP_COUNT)"
echo "OUTCOME: pass | $DB_COUNT DB integrity OK, $SKIP_COUNT atlandi ($LATEST_NAME)"
# Sessiz PASS — Telegram spam yapmasin (sadece fail bildirilir)
exit 0
