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
# Acma dizini DISKTE olmali. mktemp varsayilani /tmp = tmpfs (RAM, 14G) ve arsiv
# 12 GB aciliyordu -> tar "Cannot write: Disk quota exceeded" ile duser, script de
# bunu "tar acilamadi (corrupt?)" diye raporlardi. Yedek SAGLAMKEN 2 gun boyunca
# bozuk sanildi (2026-09-02). /var/tmp lv-root uzerinde, ~61G bos.
WORKDIR="${RESTORE_TEST_WORKDIR:-/var/tmp}"
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

# 2) Gecici dizine ac — YALNIZ *.db uyeleri.
# Asagidaki dogrulama zaten sadece .db dosyalarina bakiyor (find -name '*.db');
# arsivin tamamini acmak saf israfti: 12.07 GB yerine 2.27 GB, 13.7sn yerine 9.4sn.
# Sahte ".db" adli dosyalar da cikar ama SQLite-basligi kontrolu onlari atliyor.
mkdir -p "$WORKDIR" 2>/dev/null
TMP=$(mktemp -d -p "$WORKDIR" restore-test-XXXXXX)
trap "rm -rf '$TMP'" EXIT

# tar stderr'i AYRI yakala: nedeni siniflandirmak icin lazim. Eskiden dogrudan
# $LOG'a ekleniyordu ve script nedene bakmadan hepsine "corrupt?" diyordu.
TAR_ERR="$TMP/.tar-stderr"
if ! tar -xzf "$LATEST" -C "$TMP" --wildcards '*.db' 2>"$TAR_ERR"; then
    cat "$TAR_ERR" >> "$LOG" 2>/dev/null
    # "yer yok" ile "arsiv bozuk" AYRI arizalar — ayni mesaji vermek alarm korlugu
    # uretir (gercekten bozuk bir yedegi de "corrupt?" diye gorup ayirt edemezdik).
    if grep -qiE "no space left|quota exceeded|write error" "$TAR_ERR" 2>/dev/null; then
        AVAIL=$(df -h "$WORKDIR" 2>/dev/null | awk 'NR==2{print $4}')
        log "FAIL: acma dizininde YER YOK ($WORKDIR, bos: ${AVAIL:-?}) — arsiv bozuk DEGIL"
        echo "OUTCOME: fail | acma dizininde yer yok ($WORKDIR, bos ${AVAIL:-?}): $LATEST_NAME"
        send_telegram "🔴 *Backup Restore Test FAIL*
Acma dizininde YER YOK: \`$WORKDIR\` (bos ${AVAIL:-?})
Arsiv bozuk DEGIL — RESTORE_TEST_WORKDIR'i daha genis bir diske alin."
        exit 1
    fi
    if grep -qi "not found in archive" "$TAR_ERR" 2>/dev/null; then
        log "FAIL: arsivde hic .db uyesi yok"
        echo "OUTCOME: fail | $LATEST_NAME içinde SQLite DB yok"
        send_telegram "🔴 *Backup Restore Test FAIL*
\`$LATEST_NAME\` icinde hic .db uyesi yok!"
        exit 1
    fi
    log "FAIL: tar acilmadi (corrupt?): $(head -c 200 "$TAR_ERR" 2>/dev/null | tr '\n' ' ')"
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
