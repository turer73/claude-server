#!/bin/bash
# Pull VPS Backup — Dokploy konfig + Docker volume snapshot
# Cron: 0 4 * * * (her gece 04:00)
# Hedef: /data/backups/vps/<YYYY-MM-DD>/  (7 gün retention)
#
# VPS = root@100.126.113.23 (Tailscale-only). Klipper'dan SSH key ile bağlanır.
# Eski makinede script kayboldu; bu yeni minimal versiyon.
set -uo pipefail

source /opt/linux-ai-server/.env 2>/dev/null

VPS="${VPS_HOST:?Set VPS_HOST in .env}"
SSH="ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=20 $VPS"
LOG=/var/log/linux-ai-server/vps-backup.log
TARGET_ROOT=/data/backups/vps
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
VOLUME_PATTERN='^dokploy|n8n-data$|^plausible_db|^grafana-data$'

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
STAGE=start; VOL_OK=0; VOL_COUNT=0; VOL_SKIP=0; CH_OK=0
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
  today=$(date -u +%Y-%m-%d)
  if [ "${rts:-0}" -ge "${guard:-0}" ] 2>/dev/null; then
    line=$($SSH "grep -aE '^OUTCOME:[[:space:]]*(pass|partial|fail)' /opt/backup/logs/cron.log | tail -1" 2>/dev/null)
    if [ -n "$line" ]; then
      res=$(printf '%s' "$line" | sed -E 's/^OUTCOME:[[:space:]]*(pass|partial|fail).*/\1/')
      det=$(printf '%s' "$line" | sed -E 's/^OUTCOME:[[:space:]]*(pass|partial|fail)[[:space:]]*\|?[[:space:]]*//')
      # SIGKILL guard: backup.sh tarafı OUTCOME'a ts:YYYY-MM-DD ekleyince
      # burada today-eslesme dogrula; eski-format (ts yok) -> kontrol atla.
      ts_m=$(printf '%s' "$line" | grep -oE 'ts:[0-9]{4}-[0-9]{2}-[0-9]{2}' | head -1 | sed 's/ts://')
      if [ -n "$ts_m" ] && [ "$ts_m" != "$today" ]; then
        res=fail; det="stale-relay: OUTCOME ts=$ts_m, bugun=$today (SIGKILL/stale-log?)"
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
  elif [ "${VOL_SKIP:-1}" -gt 0 ] || [ "${CH_OK:-0}" -lt "${CH_EXPECTED:-5}" ]; then r=partial; detail="vol ${VOL_OK:-0}/${VOL_COUNT:-0} ch ${CH_OK:-0}/${CH_EXPECTED:-5}"
  else r=pass; detail="vol ${VOL_OK}/${VOL_COUNT} ch ${CH_OK}/${CH_EXPECTED} size ${TOTAL:-?}"
  fi
  echo "OUTCOME: $r | $detail"
  _relay_vps_backup  # VPS backup.sh outcome'unu da cron_outcomes'a relay et
}
trap _emit_outcome EXIT

mkdir -p "$DEST" || fail "mkdir $DEST"
log "=== START backup -> $DEST ==="

# 1. Dokploy konfig (text) — streaming, intermediate dosya yok
log "step 1/3: /etc/dokploy"
$SSH "tar -czf - -C / etc/dokploy 2>/dev/null" > "$DEST/dokploy-cfg-$DATE.tar.gz" \
  || fail "/etc/dokploy stream"

# 2. Docker volume'leri (pattern-eslestir, runtime discovery)
# Streaming: tar.gz remote'da yazilmiyor, direkt stdout uzerinden klipper'a iniyor.
# Intermediate /tmp dosyasi ve rsync round-trip yok.
VOLUMES=$($SSH "docker volume ls --format '{{.Name}}' | grep -E '$VOLUME_PATTERN'" 2>/dev/null || echo "")
VOL_COUNT=$(echo "$VOLUMES" | grep -c .)
log "step 2/3: docker volumeleri ($VOL_COUNT adet kesfedildi)"
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
  if $SSH "docker exec $CH_CONTAINER clickhouse-client --query \"SELECT * FROM $CH_DATABASE.$table FORMAT Native\" 2>/dev/null | gzip" > "$out"; then
    size=$(du -h "$out" 2>/dev/null | cut -f1)
    log "  + ch:$table OK ($size)"
    CH_OK=$((CH_OK+1))
  else
    log "  - ch:$table data FAIL"
    rm -f "$out"
  fi
done

# 3. Retention temizligi (7+ gun eski)
log "step 3/3: retention temizlik (>$RETENTION_DAYS gun)"
DELETED=$(find "$TARGET_ROOT" -maxdepth 1 -type d -mtime +$RETENTION_DAYS -print -exec rm -rf {} + 2>/dev/null | wc -l)
log "  silinen eski snapshot dizini: $DELETED"

TOTAL=$(du -sh "$DEST" 2>/dev/null | cut -f1)
STAGE=done
log "=== DONE — volumes: $VOL_OK OK / $VOL_SKIP skip, snapshot toplam: $TOTAL ==="

send_telegram "✅ *VPS Backup — $DATE*
🗂 Volumeler: $VOL_OK alındı / $VOL_SKIP yok
🦌 ClickHouse: $CH_OK tablo logical dump
📦 Toplam: \`$TOTAL\`
🗑 Eski snapshot silindi: $DELETED
🕐 \`$TS\`"

# Uptime Kuma push monitor heartbeat — basari icin "up"
kuma_push up "vol=$VOL_OK ch=$CH_OK size=$TOTAL"
