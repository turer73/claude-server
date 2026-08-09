#!/bin/bash
# db-integrity-check.sh — SQLite bozulma DETEKTORU (kesif 1462).
#
# NEDEN VAR: bozulmanin ANI bilinmiyordu. Arsiv damgasi ile dosya mtime'i saatlerce
# ayrisiyordu (ornek: server.db.corrupt-20260721-080746 -> mtime 06:23, arsivleme 08:07),
# bu yuzden "hangi is yuku ile cakisti" sorusu 6 tekrar boyunca yanitlanamadi ve butun
# korelasyon analizleri (VACUUM/cron/IO) zayif kaldi. Bu script olay penceresini SAAT'ten
# DAKIKA'ya indirir.
#
# Kanit-degeri kendini ilk calistirmada gosterdi: 2026-08-09'da bu detektorun MALIYETI
# olculurken server.db'nin o an bozuk oldugu yakalandi (oncesinde ayni gun 'ok'du).
#
# SALT-OKUMA. quick_check kullanilir (integrity_check degil): ayni bozulma sinifini
# yakalar, belirgin sekilde ucuzdur. Olculen maliyet: server.db 422M -> 779ms,
# claude_memory.db 1.1G -> 398ms. Saatlik kosmak bedava sayilir.
#
# Cron: automation/crontab (saatlik). OUTCOME marker'i cron-wrap okur.
set -uo pipefail

DATA_DIR="${DB_DATA_DIR:-/opt/linux-ai-server/data}"
LOG="${DB_INTEGRITY_LOG:-/var/log/linux-ai-server/db-integrity.log}"
EMIT="${EMIT_EVENT_BIN:-/opt/linux-ai-server/scripts/emit-event.sh}"
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

mkdir -p "$(dirname "$LOG")" 2>/dev/null

# Izlenen DB'ler. Bozuk-arsivler (*.corrupt-*, *.bak*) BILEREK haric: onlar zaten
# bozuk, her saat alarm uretirlerdi. Sadece CANLI dosyalar.
DBS=""
for f in "$DATA_DIR"/*.db; do
    case "$(basename "$f")" in
        *corrupt* | *\.bak* | *memsyn* | *preswap*) continue ;;
    esac
    [ -f "$f" ] && DBS="$DBS $f"
done

bad=0
checked=0
details=""

for db in $DBS; do
    name=$(basename "$db")
    t0=$(date +%s%N)
    # -cmd .timeout: kilitliyse hata yerine bekle (yazicilar aktifken false-alarm olmasin).
    out=$(sqlite3 -cmd ".timeout 15000" "$db" "PRAGMA quick_check;" 2>&1 | head -3)
    t1=$(date +%s%N)
    ms=$(( (t1 - t0) / 1000000 ))
    checked=$((checked + 1))
    if [ "$out" = "ok" ]; then
        echo "[$TS] OK   $name (${ms}ms)" >> "$LOG"
    else
        bad=$((bad + 1))
        first=$(printf '%s' "$out" | tr '\n' ' ' | head -c 200)
        echo "[$TS] BOZUK $name (${ms}ms): $first" >> "$LOG"
        details="${details}${name}: ${first}; "
        # Merkezi events tablosuna yaz -> notify-cron Telegram'a tasir.
        # NOT: server.db bozuksa bu INSERT de basarisiz olabilir; emit-event fail-safe,
        # log satiri YUKARIDA zaten yazildi (tek kayit-noktasina bagimli kalma).
        [ -x "$EMIT" ] && "$EMIT" alert "db-integrity:$name" critical \
            "SQLite bozulma: $name" "$first" >/dev/null 2>&1
    fi
done

if [ "$checked" -eq 0 ]; then
    echo "[$TS] UYARI: kontrol edilecek DB bulunamadi ($DATA_DIR)" >> "$LOG"
    echo "OUTCOME: partial | kontrol edilecek DB yok ($DATA_DIR)"
    exit 0
fi

if [ "$bad" -gt 0 ]; then
    # fail: cron-wrap bunu CRITICAL'e cevirir + tekrar-fail trendine girer.
    echo "OUTCOME: fail | $bad/$checked DB BOZUK — ${details}"
    exit 1
fi

echo "OUTCOME: pass | $checked DB integrity OK"
exit 0
