#!/bin/bash
# backup-docker-volumes.sh — klipper'daki CANLI docker volume'lerini yedekle.
#
# Cron: 10 3 * * * (daily-backup 03:00 sonrasi, restore-test 03:20 oncesi)
# Hedef: /backups/klipper-volumes/<YYYY-MM-DD>/  (7 gun retention)
#
# NEDEN VAR (2026-08-15, disc#1559): gozlem/otomasyon stack'i 2026-05'te
# "klipper-first" karariyla VPS'ten klipper'a tasindi. pull-vps-backup.sh o
# tarihten beri VPS'te kalan OLU volume artiklarini cekiyordu (dangling, 30
# gunde 0 yazma) — bu arada klipper'daki CANLI n8n workflow'lari, grafana
# dashboard'lari ve uptime-kuma monitorleri HIC yedeklenmiyordu. Ucu de elle
# kurulmus, yeniden uretilemez yapilandirma.
#
# SQLITE TUTARLILIGI — bu script'in asil sebebi:
# Calisan bir SQLite'i ham kopyalamak (cp/tar) TUTARSIZ olabilir: ana dosya ile
# -wal yan dosyasi FARKLI anlarda okunur, arada yazma olursa cift birbirini
# tutmaz. Bu yuzden SQLite dosyalari `.backup` (online backup API) ile
# alinir — SQLite'in kendisi tutarli bir nokta uretir — ve -wal/-shm yan
# dosyalari arsive KONMAZ (yalniz ait olduklari ana dosyayla anlamlilar; ayni
# ilke app/core/backup_manager.py:_snapshot_sqlite'ta da uygulaniyor).
#
# SQLite dosyasi ADIYLA DEGIL ICERIGIYLE bulunur (ilk 15 bayt "SQLite format 3").
# Ad'a guvenmek disc#1551'de restore-test'i kalici kirmiziya cekmisti: 18 baytlik
# bir cooldown damgasi "_server.db" adini tasiyordu.
set -uo pipefail

TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
DATE=$(date +%Y-%m-%d)
LOG=/var/log/linux-ai-server/docker-volumes-backup.log

# Override'lar YALNIZCA test icindir; cron'da tanimsiz -> uretim degerleri gecerli.
TARGET_ROOT="${VOLBACKUP_TARGET:-/backups/klipper-volumes}"
MOUNT_CHECK="${VOLBACKUP_MOUNT:-/backups}"
VOLUMES="${VOLBACKUP_VOLUMES:-n8n_n8n-data grafana-data uptime-kuma-data}"
RETENTION_DAYS="${VOLBACKUP_RETENTION:-7}"
DEST="$TARGET_ROOT/$DATE"

mkdir -p "$(dirname "$LOG")" 2>/dev/null
log() { echo "[$TS] $*" >> "$LOG"; }

VOL_OK=0
VOL_FAIL=0
VOL_TOTAL=0
STAGE=start

_emit_outcome() {
  local rc=$?
  set +e
  local r detail
  if [ "$STAGE" != "done" ]; then
    r=fail; detail="aborted rc=$rc stage=$STAGE"
  elif [ "$VOL_OK" -eq 0 ]; then
    # Hicbiri alinamadiysa yedek YOKTUR — partial degil fail.
    r=fail; detail="hic volume alinamadi (0/$VOL_TOTAL)"
  elif [ "$VOL_FAIL" -gt 0 ]; then
    r=partial; detail="vol $VOL_OK/$VOL_TOTAL ($VOL_FAIL basarisiz)"
  else
    r=pass; detail="vol $VOL_OK/$VOL_TOTAL size ${TOTAL:-?}"
  fi
  echo "OUTCOME: $r | $detail"
}
trap _emit_outcome EXIT

command -v docker >/dev/null 2>&1 || { log "FAIL: docker yok"; STAGE=nodocker; exit 1; }
command -v sqlite3 >/dev/null 2>&1 || { log "FAIL: sqlite3 yok"; STAGE=nosqlite; exit 1; }

# MOUNTPOINT GUARD — /backups fstab'da "nofail" ile duruyor (headless makinede
# mount hatasi emergency-mode'a dusurmesin). Mount yoksa buraya yazmak kok-LV'yi
# sessizce doldurur ve "yedegimiz var" yanilsamasi uretir.
if ! mountpoint -q "$MOUNT_CHECK"; then
  log "FAIL: $MOUNT_CHECK mount degil"
  STAGE=nomount
  exit 1
fi

mkdir -p "$DEST" || { log "FAIL: mkdir $DEST"; STAGE=mkdir; exit 1; }
log "=== START docker-volume backup -> $DEST ==="

for vol in $VOLUMES; do
  VOL_TOTAL=$((VOL_TOTAL + 1))
  mp=$(docker volume inspect "$vol" --format '{{.Mountpoint}}' 2>/dev/null)
  if [ -z "$mp" ] || [ ! -d "$mp" ]; then
    log "  - $vol: volume bulunamadi"
    VOL_FAIL=$((VOL_FAIL + 1))
    continue
  fi

  stage=$(mktemp -d -t volbkp-XXXXXX) || { log "  - $vol: mktemp FAIL"; VOL_FAIL=$((VOL_FAIL+1)); continue; }

  # 1) Tum agaci stage'e kopyala (SQLite'lar dahil — 2. adimda uzerine yazilacak).
  if ! cp -a "$mp/." "$stage/" 2>/dev/null; then
    log "  - $vol: kopyalama FAIL"
    rm -rf "$stage"
    VOL_FAIL=$((VOL_FAIL + 1))
    continue
  fi

  # 2) SQLite dosyalarini ICERIGE gore bul ve online-backup ile DEGISTIR.
  db_count=0
  db_bad=0
  while IFS= read -r -d '' staged; do
    # NUL-GUVENLI karsilastirma: $(...) icinde NUL bayti bash tarafindan atilir
    # ve "warning: command substitution: ignored null byte in input" basar.
    # Burada TUM agac taraniyor (ikili dosyalar dahil), o yuzden komut-ikamesi
    # yerine dogrudan bayt karsilastirmasi kullaniliyor.
    head -c 15 "$staged" 2>/dev/null | cmp -s - <(printf 'SQLite format 3') || continue
    rel="${staged#"$stage"/}"
    src="$mp/$rel"
    [ -f "$src" ] || continue
    db_count=$((db_count + 1))
    # .timeout: canli yazar varken SQLITE_BUSY ile hemen pes etme.
    if sqlite3 "$src" ".timeout 30000" ".backup '$staged'" 2>/dev/null; then
      # DOGRULA — "alindi" demek icin acilabilir olmasi yetmez.
      if [ "$(sqlite3 "$staged" 'PRAGMA integrity_check;' 2>&1 | head -1)" != "ok" ]; then
        log "  ! $vol/$rel: snapshot integrity BOZUK"
        db_bad=$((db_bad + 1))
      fi
    else
      log "  ! $vol/$rel: .backup FAIL"
      db_bad=$((db_bad + 1))
    fi
    # -wal/-shm YALNIZ ait olduklari ana dosyayla anlamli; snapshot sonrasi
    # bayat kalirlar ve restore'da tutarsizlik uretirler -> arsive konmaz.
    rm -f "$staged-wal" "$staged-shm" "$staged-journal"
  done < <(find "$stage" -type f -print0 2>/dev/null)

  if [ "$db_bad" -gt 0 ]; then
    log "  - $vol: $db_bad/$db_count DB snapshot basarisiz -> arsivlenmedi"
    rm -rf "$stage"
    VOL_FAIL=$((VOL_FAIL + 1))
    continue
  fi

  # 3) Stage'i tar'la (artik icerideki her SQLite tutarli bir snapshot).
  out="$DEST/$vol-$DATE.tar.gz"
  if tar -czf "$out" -C "$stage" . 2>/dev/null && gzip -t "$out" 2>/dev/null; then
    size=$(du -h "$out" 2>/dev/null | cut -f1)
    log "  + $vol: OK ($size, $db_count SQLite snapshot)"
    VOL_OK=$((VOL_OK + 1))
  else
    log "  - $vol: tar/gzip FAIL"
    rm -f "$out"
    VOL_FAIL=$((VOL_FAIL + 1))
  fi
  rm -rf "$stage"
done

# Retention
DELETED=$(find "$TARGET_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +"$RETENTION_DAYS" -print 2>/dev/null | wc -l)
find "$TARGET_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +"$RETENTION_DAYS" -exec rm -rf {} + 2>/dev/null
TOTAL=$(du -sh "$DEST" 2>/dev/null | cut -f1)
STAGE=done
log "=== DONE — vol $VOL_OK OK / $VOL_FAIL fail, toplam $TOTAL, silinen eski: $DELETED ==="
exit 0
