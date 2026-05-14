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
SSH="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=20 $VPS"
LOG=/var/log/linux-ai-server/vps-backup.log
TARGET_ROOT=/data/backups/vps
RETENTION_DAYS=7
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
DATE=$(date +%Y-%m-%d)
DEST="$TARGET_ROOT/$DATE"

# Yedek alinacak volume'ler — VPS'te runtime'da kesfet (Dokploy isimleri
# deploy ID'siyle prefix alabilir, statik liste kirilgan). Pattern:
#   - dokploy* (postgres, redis, traefik konfig)
#   - *n8n-data (Dokploy UUID prefix'li)
#   - plausible_* (db + events)
#   - grafana-data
VOLUME_PATTERN='^dokploy|n8n-data$|^plausible|^grafana-data$'

send_telegram() {
  curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d chat_id="$TELEGRAM_CHAT_ID" -d parse_mode="Markdown" -d text="$1" >/dev/null 2>&1
}

log()  { echo "[$TS] $*" >> "$LOG"; }
fail() { log "FAIL: $*"; send_telegram "🔴 *VPS Backup BAŞARISIZ*
\`$TS\`
$1"; exit 1; }

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

# 3. Retention temizligi (7+ gun eski)
log "step 3/3: retention temizlik (>$RETENTION_DAYS gun)"
DELETED=$(find "$TARGET_ROOT" -maxdepth 1 -type d -mtime +$RETENTION_DAYS -print -exec rm -rf {} + 2>/dev/null | wc -l)
log "  silinen eski snapshot dizini: $DELETED"

TOTAL=$(du -sh "$DEST" 2>/dev/null | cut -f1)
log "=== DONE — volumes: $VOL_OK OK / $VOL_SKIP skip, snapshot toplam: $TOTAL ==="

send_telegram "✅ *VPS Backup — $DATE*
🗂 Volumeler: $VOL_OK alındı / $VOL_SKIP yok
📦 Toplam: \`$TOTAL\`
🗑 Eski snapshot silindi: $DELETED
🕐 \`$TS\`"
