#!/bin/bash
# outcome.sh — klipper-cron-wrap OUTCOME-contract yardımcıları (echo-only, YAN ETKİSİZ).
#
# Amaç (LIVESYS-SENSE / duyusal-dürüstlük): cron-script'leri "kör-tarama ≠ temiz-tarama"
# ayrımını OUTCOME marker'ıyla beyan etsin; wrapper rc-fallback'e binip sessiz-yeşil
# raporlamasın. Bu dosya SADECE stdout'a yazar / rc döner — hiçbir dosya/DB/servis
# durumuna dokunmaz, source edilince güvenlidir.
#
# Kullanım:
#   . "$(dirname "$0")/../scripts/lib/outcome.sh"   # veya mutlak yol
#   emit_outcome pass "12 domain tarandı, 0 bulgu"
#   res=$(numeric_floor "$executed" "$total"); emit_outcome "$res" "..."
#   json_floor results.json || emit_outcome fail "rapor geçersiz/boş"

# emit_outcome <result> <detail...>
#   "OUTCOME: <result> | <detail>" basar. result pass|partial|fail değilse FAIL'e zorlar
#   (geçersiz/boş sonuç sessiz-yeşile dönmesin — duyusal-dürüstlük ilkesi).
emit_outcome() {
    local result="${1:-fail}"
    shift 2>/dev/null || true
    case "$result" in
        pass | partial | fail) ;;
        *) result="fail" ;;
    esac
    printf 'OUTCOME: %s | %s\n' "$result" "$*"
}

# numeric_floor <executed> <total>
#   Çalıştırılan-iş tabanına göre sonucu echo'lar: hiçbiri çalışmadıysa FAIL
#   (kör-tarama), bir kısmı çalıştıysa PARTIAL, hepsi çalıştıysa PASS.
#   Sayı-olmayan/boş girdi → 0 sayılır → güvenli yön (fail). echo: pass|partial|fail
numeric_floor() {
    local executed="${1:-0}" total="${2:-0}"
    # sayı-olmayan/boş → 0 (güvenli yön: fail). case-glob non-digit veya boşu yakalar.
    case "$executed" in '' | *[!0-9]*) executed=0 ;; esac
    case "$total" in '' | *[!0-9]*) total=0 ;; esac
    # executed<=0 (hiç çalışmadı) VEYA total<=0 (toplam belirlenemedi/eksik-parse) → fail.
    # total geçersizken executed>0 olsa bile pass DEME (safe-fail; Codex P2).
    if [ "$executed" -le 0 ] || [ "$total" -le 0 ]; then
        printf 'fail'
    elif [ "$executed" -lt "$total" ]; then
        printf 'partial'
    else
        printf 'pass'
    fi
}

# json_floor <file>
#   Dosya VAR + boş-değil + geçerli-JSON mu? rc 0 (sağlam) / rc 1 (eksik/bozuk/boş).
#   Yan etkisiz; parse-öncesi guard olarak kullanılır (runner çöküşü = bozuk-JSON).
json_floor() {
    local f="${1:-}"
    [ -n "$f" ] && [ -s "$f" ] || return 1
    python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$f" 2>/dev/null
}
