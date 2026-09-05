#!/bin/bash
# Pull VPS Backup — Dokploy konfig + Docker volume snapshot + Postgres SQL dump
# Cron: 0 4 * * * (her gece 04:00)
# Hedef: /backups/vps/<YYYY-MM-DD>/  (7 gün retention)
#
# VPS = root@100.126.113.23 (Tailscale-only). Klipper'dan SSH key ile bağlanır.
# Eski makinede script kayboldu; bu yeni minimal versiyon.
set -uo pipefail

source /opt/linux-ai-server/.env 2>/dev/null

VPS="${VPS_HOST:?Set VPS_HOST in .env}"
SSH="ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=20 $VPS"
LOG=/var/log/linux-ai-server/vps-backup.log
# 2026-08-15: eski hedef /datasets/backups/vps idi; o LV Lexar NM790'daydi ve disk
# 2026-08-11'de namespace-seviyesinde oldu (kesif #1549) -> hedef yok oldu, script
# "mkdir: Input/output error" ile dusuyordu. Yeni hedef vg-storage/lv-backup (30G).
# UYARI: makinede artik TEK disk var (Crucial P3). Bu yedek veriyle AYNI fiziksel
# diskte duruyor; disk arizasina karsi koruma SAGLAMAZ, yalnizca mantiksal
# (silme/bozulma/VPS-kaybi) senaryolarini korur. Off-site kopya hala eksik.
# Override'lar YALNIZCA test icindir (tests/test_pull_vps_backup_contract.py).
# Cron ortaminda bu degiskenler tanimsizdir -> uretim degerleri aynen gecerli.
TARGET_ROOT="${VPS_BACKUP_TARGET:-/backups/vps}"
# Guard'in bakacagi mount noktasi. Test bunu gercekten mount'lu bir yola
# (ornegin /) isaret ettirir; guard DEVRE DISI BIRAKILMAZ, yalnizca
# saglanabilir bir hedef verilir.
MOUNT_CHECK="${VPS_BACKUP_MOUNT:-/backups}"
RETENTION_DAYS=7
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
DATE=$(date +%Y-%m-%d)
DEST="$TARGET_ROOT/$DATE"

# Yedek alinacak volume'ler — VPS'te runtime'da kesfet. Pattern:
#   - dokploy* (postgres, redis, traefik konfig)
#   - *n8n-data (Dokploy UUID prefix'li)
#   - plausible_db* (Postgres user/site meta)
#   - grafana-data
# plausible_event-data (ClickHouse) volume tar etmiyoruz — 449MB'i sistem
# log/WAL, gercek data sadece 3.4 MiB. Logical dump asagida (step 2.5).
#
# n8n-data ve grafana-data 2026-08-15'te PATTERN'DAN CIKARILDI (disc#1565).
# Olculdu: VPS'te n8n/grafana KONTEYNERI YOK (docker ps -a bos), her iki volume
# de DANGLING ve son 30 gunde 0 dosya degismis (mtime'lar 2026-05-12/13).
# Sebep: gozlem/otomasyon stack'i 2026-05'te "klipper-first" karariyla klipper'a
# tasindi; bunlar o tasimadan kalma ARTIK. Gecelik 24MB olu veri cekiliyordu,
# CANLI olanlar ise klipper'da ve HIC yedeklenmiyordu -> yeni script:
# automation/backup-docker-volumes.sh (SQLite online-backup ile).
# Cekmeyi durdurmadan once son kopya alindi: /backups/archive/vps-orphan-volumes-20260815/
VOLUME_PATTERN='^dokploy|^plausible_db'

# CANLI POSTGRES VERI DIZINI TAR'LANMAZ (2026-08-15).
# Calisan bir Postgres'in data dizinini pg_start_backup/WAL-arsivi olmadan tar
# etmek TUTARLI bir yedek DEGILDIR — restore'da bozuk cikabilir. Ustelik tar,
# dosya okunurken degistigi icin ("file changed as we read it") exit 1 verip
# ARALIKLI dusuyordu: plausible_db-data 2026-08-15'te "stream FAIL" verdi, elle
# uc denemenin ucu de basarili oldu -> uretilemedi, cunku yaris zamanlamaya bagli.
# DOGRU KAYNAK ZATEN VAR: VPS'in kendi /opt/backup/backup.sh'i (cron 03:00)
# pg_dump/pg_dumpall ile mantiksal dump uretiyor; step 2.6 onlari cekiyor.
# Bu volume'ler o yuzden ATLANIR — eksiklik degil, dogru kaynaga yonlendirme.
# VOLUME_PATTERN bilerek GENIS birakildi ki yeni bir dokploy-* volume kesfedilsin.
PG_VOLUME_SKIP='^dokploy-postgres$|^plausible_db'

# VPS'in mantiksal dump dizini (backup.sh yazar, orada LOCAL_RETENTION=3 gun).
# klipper 7 gun tuttugu icin bu cekme VPS'in kendi retention'ini UZATIR.
VPS_DUMP_ROOT=/opt/backup/data
# Dump bu yastan eskiyse "yedek var" demek yanlis olur -> sayilmaz + raporlanir.
DUMP_MAX_AGE_HOURS=36

# ClickHouse Plausible event tablolari (her biri Native format, gzip)
CH_CONTAINER='plausible-plausible_events_db-1'
CH_DATABASE='plausible_events_db'
CH_TABLES='events_v2 sessions_v2 location_data ingest_counters schema_migrations'

send_telegram() {
  curl --max-time 15 --connect-timeout 5 -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d chat_id="$TELEGRAM_CHAT_ID" -d parse_mode="Markdown" -d text="$1" >/dev/null 2>&1
}

log()  { echo "[$TS] $*" >> "$LOG"; }

kuma_push() {
  # Uptime Kuma push monitor heartbeat. status=up|down, msg url-encoded.
  [ -z "${KUMA_BACKUP_PUSH_URL:-}" ] && return 0
  local status="$1" msg="${2:-}"
  curl -fsS --max-time 5 "${KUMA_BACKUP_PUSH_URL}?status=${status}&msg=$(printf %s "$msg" | jq -sRr @uri 2>/dev/null || echo OK)" >/dev/null 2>&1 || true
}

fail() {
  log "FAIL: $*"
  send_telegram "🔴 *VPS Backup BAŞARISIZ*
\`$TS\`
$1"
  kuma_push down "$1"
  exit 1
}

# LIVESYS Faz1 outcome-contract: gercek sonuc EXIT-trap ile (set -e/abort durumunda bile emit)
CH_EXPECTED=$(echo $CH_TABLES | wc -w)
STAGE=start; VOL_OK=0; VOL_COUNT=0; VOL_SKIP=0; CH_OK=0; SQL_OK=0; SQL_STALE=0; PG_SKIPPED=0
# VPS backup.sh ciplak-cron'da kosar (klipper-cron-wrap YOK) -> OUTCOME'u yalniz
# /opt/backup/logs/cron.log'a (backup-exclusive, cumulative) gider. Buradan cekip
# merkezi cron_outcomes'a job='vps-backup-push' relay et (consumer-gap secenek a).
# Tazelik-guard (cumulative-log oldugu icin SART, Codex-dersi/bu-run-bagli):
# cron.log bugun 02:55'ten sonra yazilmali (bugunku 03:00 run), degilse stale->fail.
_relay_vps_backup() {
  set +e
  local db="${DB_PATH:-/opt/linux-ai-server/data/server.db}"
  [ -f "$db" ] || return 0
  local rts guard line res det safe today ts_m
  rts=$($SSH "stat -c %Y /opt/backup/logs/cron.log 2>/dev/null || echo 0" 2>/dev/null)
  guard=$(date -d 'today 02:55' +%s 2>/dev/null || echo 0)
  # YEREL tarih: VPS de klipper de Europe/Istanbul ve backup.sh ts'i YEREL yaziyor;
  # date -u ile kiyas 21:00-00:00 arasi bir gun kayardi (mtime guard'i zaten yerel).
  today=$(date +%Y-%m-%d)
  if [ "${rts:-0}" -ge "${guard:-0}" ] 2>/dev/null; then
    line=$($SSH "grep -aE '^OUTCOME:[[:space:]]*(pass|partial|fail)' /opt/backup/logs/cron.log | tail -1" 2>/dev/null)
    if [ -n "$line" ]; then
      res=$(printf '%s' "$line" | sed -E 's/^OUTCOME:[[:space:]]*(pass|partial|fail).*/\1/')
      det=$(printf '%s' "$line" | sed -E 's/^OUTCOME:[[:space:]]*(pass|partial|fail)[[:space:]]*\|?[[:space:]]*//')
      # SIGKILL guard: backup.sh tarafı OUTCOME'a ts:YYYY-MM-DD ekleyince
      # burada today-eslesme dogrula; eski-format (ts yok) -> kontrol atla.
      ts_m=$(printf '%s' "$line" | grep -oE 'ts:[0-9]{4}-[0-9]{2}-[0-9]{2}' | head -1 | sed 's/ts://')
      if [ -n "$ts_m" ] && [ "$ts_m" != "$today" ]; then
        # URETICI-TUKETICI YARISI (2026-09-05): "hala kosuyor" ile "bitti ama sonuc
        # yok" AYRI arizalar, tek mesaja gomulmesinler (PR#377 dersi). VPS backup
        # suresi 15dk'dan 111dk'ya cikti; 04:20'de okuyunca uretici henuz bitmemisti
        # ve bu "SIGKILL/stale-log?" diye CRITICAL raporlandi — yedek sagIamdi.
        if $SSH "pgrep -f '/opt/backup/backup\.sh' >/dev/null 2>&1" 2>/dev/null; then
          res=partial; det="VPS backup HALA KOSUYOR: bugunku sonuc henuz yok (son tamamlanan ts=$ts_m)"
        else
          res=fail; det="stale-relay: OUTCOME ts=$ts_m, bugun=$today, backup.sh KOSMUYOR (SIGKILL/stale-log?)"
        fi
      fi
    else
      res=fail; det="cron.log taze ama OUTCOME yok (trap-oncesi/eksik run?)"
    fi
  else
    res=fail; det="stale: cron.log mtime eski, bugun VPS backup kosmadi"
  fi
  safe="$(printf '%s' "$det" | tr -d '\\`"' | tr '\n\r\t' '   ' | head -c 300)"; safe="${safe//\'/\'\'}"
  sqlite3 "$db" "INSERT INTO cron_outcomes (job,result,rc,source,detail) VALUES ('vps-backup-push','${res:-fail}',0,'relay','$safe');" 2>/dev/null || true
  if [ "${res:-fail}" != "pass" ]; then
    local bsev=critical
    [ "$res" = "partial" ] && bsev=warning
    /opt/linux-ai-server/scripts/emit-event.sh "backup" "vps:backup-push" "$bsev" "VPS backup ${res:-fail}" "$det"
  fi
}

_emit_outcome() {
  local rc=$?
  set +e
  local r detail
  if [ "${STAGE:-start}" != "done" ]; then r=fail; detail="aborted rc=$rc stage=${STAGE:-start}"
  # SQL_OK=0 ZAYIF bir "partial" degil, FAIL: Postgres'in tek gecerli yedegi
  # mantiksal dump (volume tar'i bilerek atlaniyor, bkz PG_VOLUME_SKIP). Hic
  # dump inmediyse elimizde Postgres yedegi YOKTUR ve bu gurultuye gomulmemeli.
  elif [ "${SQL_OK:-0}" -eq 0 ]; then r=fail; detail="POSTGRES YEDEGI YOK: sql 0 alindi (stale ${SQL_STALE:-0}) — VPS backup.sh kosuyor mu?"
  elif [ "${VOL_SKIP:-1}" -gt 0 ] || [ "${CH_OK:-0}" -lt "${CH_EXPECTED:-5}" ] || [ "${SQL_STALE:-0}" -gt 0 ]; then r=partial; detail="vol ${VOL_OK:-0}/${VOL_COUNT:-0} ch ${CH_OK:-0}/${CH_EXPECTED:-5} sql ${SQL_OK:-0} stale ${SQL_STALE:-0}"
  else r=pass; detail="vol ${VOL_OK}/${VOL_COUNT} ch ${CH_OK}/${CH_EXPECTED} sql ${SQL_OK} pg-skip ${PG_SKIPPED:-0} size ${TOTAL:-?}"
  fi
  echo "OUTCOME: $r | $detail"
  _relay_vps_backup  # VPS backup.sh outcome'unu da cron_outcomes'a relay et
}
trap _emit_outcome EXIT

# MOUNTPOINT GUARD — fstab'da /backups "nofail" ile duruyor (headless makinede
# mount hatasi emergency-mode'a dusurmesin diye, bkz /etc/fstab yorumu). Bunun
# karsi-riski: mount yoksa /backups kok-LV uzerinde sirali bir dizin olur ve bu
# script her gece oraya yazip kok diski sessizce doldurur -- ustelik "yedek var"
# yanilsamasi uretir. Yazmadan ONCE gercekten mount'lu mu dogrula.
if ! mountpoint -q "$MOUNT_CHECK"; then
  fail "$MOUNT_CHECK mount DEGIL (vg-storage/lv-backup dusmus?) — kok-LV'ye yazmamak icin durduruldu"
fi

mkdir -p "$DEST" || fail "mkdir $DEST"
log "=== START backup -> $DEST ==="

# 1. Dokploy konfig (text) — streaming, intermediate dosya yok
log "step 1/3: /etc/dokploy"
$SSH "tar -czf - -C / etc/dokploy 2>/dev/null" > "$DEST/dokploy-cfg-$DATE.tar.gz" \
  || fail "/etc/dokploy stream"

# 2. Docker volume'leri (pattern-eslestir, runtime discovery)
# Streaming: tar.gz remote'da yazilmiyor, direkt stdout uzerinden klipper'a iniyor.
# Intermediate /tmp dosyasi ve rsync round-trip yok.
VOLUMES_ALL=$($SSH "docker volume ls --format '{{.Name}}' | grep -E '$VOLUME_PATTERN'" 2>/dev/null || echo "")
# Postgres veri-dizini volume'lerini AYIR (yukaridaki PG_VOLUME_SKIP gerekcesi).
# Sayimdan da dusuluyor: bilincli atlama "skip" degildir, yoksa OUTCOME kalici
# olarak partial'a takilir ve gercek eksik yedekler bu gurultude kaybolur.
VOLUMES=$(printf '%s\n' "$VOLUMES_ALL" | grep -vE "$PG_VOLUME_SKIP" || true)
PG_SKIPPED=$(printf '%s\n' "$VOLUMES_ALL" | grep -cE "$PG_VOLUME_SKIP" || true)
VOL_COUNT=$(printf '%s\n' "$VOLUMES" | grep -c . || true)
log "step 2/3: docker volumeleri ($VOL_COUNT tar'lanacak, $PG_SKIPPED Postgres volume'u atlandi -> mantiksal dump step 2.6)"
# Sessiz-eleme yok: neyin neden atlandigi log'a tek tek yazilir.
printf '%s\n' "$VOLUMES_ALL" | grep -E "$PG_VOLUME_SKIP" | while read -r skipped; do
  [ -n "$skipped" ] && log "  ~ $skipped: ATLANDI (canli Postgres data dir; tutarsiz yedek uretir)"
done
VOL_OK=0
for vol in $VOLUMES; do
  [ -z "$vol" ] && continue
  mountpoint=$($SSH "docker volume inspect '$vol' --format '{{.Mountpoint}}'" 2>/dev/null)
  [ -z "$mountpoint" ] && { log "  - $vol: inspect FAIL"; continue; }
  parent=$(dirname "$mountpoint")
  base=$(basename "$mountpoint")
  out="$DEST/vol-$vol-$DATE.tar.gz"
  if $SSH "tar -czf - -C $parent $base 2>/dev/null" > "$out"; then
    size=$(du -h "$out" 2>/dev/null | cut -f1)
    log "  + $vol: OK ($size)"
    VOL_OK=$((VOL_OK+1))
  else
    log "  - $vol: stream FAIL"
    rm -f "$out"
  fi
done
VOL_SKIP=$((VOL_COUNT - VOL_OK))

# VPS'te birikmis eski temp tar.gz'leri temizle (onceki crashed run'lardan).
$SSH "rm -f /tmp/vol-*-*.tar.gz /tmp/dokploy-cfg-*.tar.gz 2>/dev/null" || true

# 2.5. ClickHouse Plausible event_data logical dump
#  Native format + gzip stream. Volume tarball'dan ~100x kucuk, restore icin
#  schema ile birlikte alinir.
log "step 2.5: ClickHouse $CH_DATABASE (logical)"
mkdir -p "$DEST/clickhouse"
CH_OK=0
for table in $CH_TABLES; do
  # Schema (CREATE TABLE)
  $SSH "docker exec $CH_CONTAINER clickhouse-client --query \"SHOW CREATE TABLE $CH_DATABASE.$table FORMAT TabSeparatedRaw\" 2>/dev/null" \
    > "$DEST/clickhouse/$table.schema.sql" || { log "  - ch:$table schema FAIL"; rm -f "$DEST/clickhouse/$table.schema.sql"; continue; }
  # Data (Native binary, gzip)
  out="$DEST/clickhouse/$table.native.gz"
  # `gzip -t` SART: ssh/docker cagrisi rc=0 donse bile akis yarim inmis ya da
  # hic veri gelmemis olabilir; o zaman 0-baytlik dosya "OK" sayiliyordu
  # (2026-08-15'te stub'li testte gorundu: gercek veri yokken ch 5/5 raporlandi).
  # Not: GERCEKTEN bos bir tablo gecerli bir gzip uretir -> dogru sekilde OK sayilir.
  if $SSH "docker exec $CH_CONTAINER clickhouse-client --query \"SELECT * FROM $CH_DATABASE.$table FORMAT Native\" 2>/dev/null | gzip" > "$out" && gzip -t "$out" 2>/dev/null; then
    size=$(du -h "$out" 2>/dev/null | cut -f1)
    log "  + ch:$table OK ($size)"
    CH_OK=$((CH_OK+1))
  else
    log "  - ch:$table data FAIL"
    rm -f "$out"
  fi
done

# 2.6. Postgres MANTIKSAL dump'lari — VPS'in kendi backup.sh'inden cek.
#  Neden burada uretmiyoruz: VPS zaten cron 03:00'te pg_dump/pg_dumpall aliyor
#  (plausible_db, dokploy_db=pg_dumpall, panola_db, bilge-arena). Ayni dump'i
#  ikinci kez uretmek DB'ye gereksiz yuk bindirir ve iki farkli zaman noktasi
#  yaratir. Bizim eksigimiz uretim degil, OFF-VPS KOPYA idi — cektigimiz sey bu.
#  Yan fayda: VPS'te LOCAL_RETENTION=3 gun, klipper 7 gun tutuyor -> retention uzuyor.
log "step 2.6: VPS mantiksal SQL dump'lari"
mkdir -p "$DEST/sql"
SQL_OK=0
SQL_STALE=0
SQL_SRC=$($SSH "ls -1dt $VPS_DUMP_ROOT/*/ 2>/dev/null | head -1" | tr -d '\r' | sed 's:/*$::')
if [ -z "$SQL_SRC" ]; then
  log "  - VPS dump dizini bulunamadi ($VPS_DUMP_ROOT) — backup.sh hic kosmamis olabilir"
else
  log "  kaynak: $SQL_SRC"
  # TAZELIK GUARD: bayat dump'i sessizce cekmek "yedegimiz var" yanilsamasi
  # uretir. Yasi VPS'te olcuyoruz (klipper'a inince mtime yenilenirdi).
  SQL_FILES=$($SSH "find '$SQL_SRC' -maxdepth 1 -name '*.sql.gz' -type f -printf '%f\n' 2>/dev/null" | tr -d '\r')
  for f in $SQL_FILES; do
    [ -z "$f" ] && continue
    fresh=$($SSH "find '$SQL_SRC/$f' -mmin -$((DUMP_MAX_AGE_HOURS * 60)) 2>/dev/null | head -1" | tr -d '\r')
    if [ -z "$fresh" ]; then
      log "  ~ $f: BAYAT (>${DUMP_MAX_AGE_HOURS}h) — cekilmedi, VPS backup.sh kosmuyor olabilir"
      SQL_STALE=$((SQL_STALE+1))
      continue
    fi
    out="$DEST/sql/$f"
    if $SSH "cat '$SQL_SRC/$f'" > "$out" 2>/dev/null && gzip -t "$out" 2>/dev/null; then
      size=$(du -h "$out" 2>/dev/null | cut -f1)
      log "  + sql:$f OK ($size)"
      SQL_OK=$((SQL_OK+1))
    else
      # gzip -t: yarim inen dosyayi "alindi" saymayalim.
      log "  - sql:$f cekme/butunluk FAIL"
      rm -f "$out"
    fi
  done
fi
if [ "$SQL_OK" -eq 0 ]; then
  log "  UYARI: hic mantiksal dump alinamadi — Postgres yedegi YOK sayilmali"
fi

# 3. Retention temizligi (7+ gun eski)
log "step 3/3: retention temizlik (>$RETENTION_DAYS gun)"
DELETED=$(find "$TARGET_ROOT" -maxdepth 1 -type d -mtime +$RETENTION_DAYS -print -exec rm -rf {} + 2>/dev/null | wc -l)
log "  silinen eski snapshot dizini: $DELETED"

TOTAL=$(du -sh "$DEST" 2>/dev/null | cut -f1)
STAGE=done
log "=== DONE — volumes: $VOL_OK OK / $VOL_SKIP skip ($PG_SKIPPED pg-atlandi), sql: $SQL_OK OK / $SQL_STALE bayat, ch: $CH_OK, toplam: $TOTAL ==="

send_telegram "✅ *VPS Backup — $DATE*
🗂 Volumeler: $VOL_OK alındı / $VOL_SKIP yok ($PG_SKIPPED Postgres → SQL dump)
🐘 Postgres SQL dump: $SQL_OK alındı / $SQL_STALE bayat
🦌 ClickHouse: $CH_OK tablo logical dump
📦 Toplam: \`$TOTAL\`
🗑 Eski snapshot silindi: $DELETED
🕐 \`$TS\`"

# Uptime Kuma push monitor heartbeat — basari icin "up"
kuma_push up "vol=$VOL_OK sql=$SQL_OK ch=$CH_OK size=$TOTAL"
