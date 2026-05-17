#!/bin/bash
# edge-log-redact.sh — KVKK uyumlu edge access log pseudonymization.
#
# Akis:
#   1. /var/log/edge/traefik-access.log + /var/log/caddy/access.log icindeki
#      14 gunden eski JSON satirlarinda ClientHost (Traefik) / request.remote_ip
#      (Caddy) alanlarini SHA256(salt + IP) ile pseudonymize eder.
#   2. 90 gunden eski satirlari tamamen siler.
#
# Hukuki dayanak: KVKK Madde 5(2)(f) mesru menfaat (sistem guvenligi).
# Aydinlatma yukumlulugu (Madde 10): /privacy sayfasinda log isleme aciklamasi
# olmali — bu cron aktiflesmeden once dogrula.
#
# Bu script VPS'te calisir (loglar VPS'te). klipper-cron-wrap.sh uzerinden
# tetiklenir veya VPS'te ayri cron entry kurulur.

set -euo pipefail

SALT_FILE="/etc/edge-log-salt"
RETAIN_FULL_DAYS=14   # IP raw kalir
RETAIN_HASH_DAYS=90   # IP hash'lenmis kalir; sonra satir silinir

LOGS=(
    "/var/log/edge/traefik-access.log:ClientHost"
    "/var/log/caddy/access.log:request.remote_ip"
)

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*"; }

if [ ! -r "$SALT_FILE" ]; then
    log "ERROR: salt dosyasi okunamiyor: $SALT_FILE (chmod 600 + root owner)"
    exit 1
fi
SALT=$(cat "$SALT_FILE")
if [ -z "$SALT" ] || [ "${#SALT}" -lt 16 ]; then
    log "ERROR: salt bos veya cok kisa (>=16 char gerekli)"
    exit 1
fi

CUTOFF_HASH=$(date -d "$RETAIN_FULL_DAYS days ago" -u +%s)
CUTOFF_DELETE=$(date -d "$RETAIN_HASH_DAYS days ago" -u +%s)

redact_file() {
    local file="$1"
    local ip_field="$2"

    if [ ! -f "$file" ]; then
        log "SKIP: $file yok"
        return 0
    fi

    local tmp="${file}.redact.tmp"
    : > "$tmp"

    local total=0 hashed=0 deleted=0 kept=0

    while IFS= read -r line; do
        total=$((total + 1))

        # JSON satirindan timestamp cek (Traefik: StartUTC, Caddy: ts)
        local row_ts
        row_ts=$(echo "$line" | python3 -c "
import json, sys, datetime
try:
    d = json.loads(sys.stdin.read())
    t = d.get('StartUTC') or d.get('ts') or d.get('time')
    if isinstance(t, (int, float)):
        print(int(t))
    elif isinstance(t, str):
        # ISO8601 parse
        t = t.replace('Z', '+00:00')
        print(int(datetime.datetime.fromisoformat(t).timestamp()))
    else:
        print(0)
except Exception:
    print(0)
" 2>/dev/null || echo 0)

        if [ "$row_ts" = "0" ]; then
            # Timestamp parse edilemedi, satir aynen kalir (savunmaci)
            echo "$line" >> "$tmp"
            kept=$((kept + 1))
            continue
        fi

        if [ "$row_ts" -lt "$CUTOFF_DELETE" ]; then
            # 90 gunden eski - sil
            deleted=$((deleted + 1))
            continue
        fi

        if [ "$row_ts" -lt "$CUTOFF_HASH" ]; then
            # 14-90 gun arasi - IP hash'le
            local hashed_line
            hashed_line=$(SALT_VAL="$SALT" FIELD="$ip_field" echo "$line" | python3 -c "
import json, sys, hashlib, os
try:
    d = json.loads(sys.stdin.read())
    salt = os.environ['SALT_VAL']
    field = os.environ['FIELD']
    # Nested field support (request.remote_ip)
    parts = field.split('.')
    target = d
    for p in parts[:-1]:
        target = target.get(p, {})
        if not isinstance(target, dict):
            target = {}
            break
    leaf = parts[-1]
    ip = str(target.get(leaf, ''))
    if ip and not ip.startswith('REDACTED_'):
        h = hashlib.sha256((salt + ip).encode()).hexdigest()[:16]
        target[leaf] = 'REDACTED_' + h
    print(json.dumps(d, separators=(',', ':'), ensure_ascii=False))
except Exception as e:
    # Hata: orijinal satir korunur
    sys.stdout.write(sys.stdin.read())
" 2>/dev/null) || hashed_line="$line"
            echo "$hashed_line" >> "$tmp"
            hashed=$((hashed + 1))
        else
            # 14 gunden yeni - aynen tut
            echo "$line" >> "$tmp"
            kept=$((kept + 1))
        fi
    done < "$file"

    mv "$tmp" "$file"
    log "REDACT $file: total=$total kept=$kept hashed=$hashed deleted=$deleted"
}

for entry in "${LOGS[@]}"; do
    file="${entry%%:*}"
    field="${entry##*:}"
    redact_file "$file" "$field"
done

log "done"
