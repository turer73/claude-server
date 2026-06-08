#!/bin/bash
# livesys-canary.sh — Alarm-yolu sentetik canary'si (LIVESYS-SENSE).
#
# liveness.py YALNIZ process/veri-canlılığını ölçer; alarm PIPELINE'ının DOĞRULUĞUNU
# (bir 'fail' gerçekten cron_outcomes'a fail-satırı olarak düşüyor mu) sınamaz. Bu canary:
#   1. known-good (OUTCOME:pass) + known-bad (OUTCOME:fail) işini GERÇEK klipper-cron-wrap'ten
#      geçirir (CANARY_SUPPRESS_ALERT=1 → cron_outcomes yazılır ama GERÇEK alarm/event YOK,
#      sentetik known-bad spam yapmaz),
#   2. cron_outcomes'ta beklenen pass/fail satırı oluştu mu SELECT ile teyit eder,
#   3. kendi test-satırlarını SİLER (prod-event'e dokunmaz; job-adı PID'li → çakışma yok),
#   4. pipeline sağlamsa OUTCOME:pass, bozuksa OUTCOME:fail (canary'nin KENDİ outcome'u
#      normal yoldan akar → pipeline bozuksa gerçek alarm verir; canary'nin amacı budur).
#
# canli-notify default KAPALI: sentetik enjeksiyonlar CANARY_SUPPRESS_ALERT ile sessiz;
# yalnız canary'nin kendi sonucu (pipeline-bozuk) alarm üretebilir.
set -uo pipefail

# ROOT'u SCRIPT-konumundan türet (kardeş lint-cron-outcome.sh deseni) — hardcoded /opt
# CI/test/normal-checkout'ta source-fail ediyordu (B1). LIVESYS_ROOT ile override edilebilir.
ROOT="${LIVESYS_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# KEMER-KAYIŞ: outcome.sh source edilemezse (eksik/taşınmış) canary'nin KENDİSİ silent-green
# olmasın → açıkça OUTCOME:fail emit edip çık (sensory-honesty ironisini kapat).
. "$ROOT/scripts/lib/outcome.sh" 2>/dev/null || {
    echo "OUTCOME: fail | outcome.sh source-fail ($ROOT/scripts/lib/outcome.sh yok) — canary çalışamadı"
    exit 0
}
DB_PATH="${DB_PATH:-$ROOT/data/server.db}"
WRAP="${WRAP:-$ROOT/scripts/klipper-cron-wrap.sh}"  # test fixture için override edilebilir
OK_JOB="livesys-canary-ok-$$"
BAD_JOB="livesys-canary-bad-$$"

# Test-artıklarını her durumda temizle: cron_outcomes test-satırları + wrapper'ın yazdığı
# PID-scoped per-run LOG dosyaları (R1: yoksa /var/log'da günlük 2 dosya birikir, rotate-yok).
CRON_LOG_DIR="${CRON_LOG_DIR:-/var/log/linux-ai-server}"
cleanup() {
    [ -f "$DB_PATH" ] && sqlite3 -cmd ".timeout 10000" "$DB_PATH" \
        "DELETE FROM cron_outcomes WHERE job IN ('$OK_JOB','$BAD_JOB');" 2>/dev/null || true
    rm -f "$CRON_LOG_DIR/${OK_JOB}.log" "$CRON_LOG_DIR/${BAD_JOB}.log" 2>/dev/null || true
}
trap cleanup EXIT

if [ ! -f "$DB_PATH" ]; then
    emit_outcome fail "server.db yok ($DB_PATH) — alarm-yolu doğrulanamadı"
    exit 0
fi

# known-good + known-bad → gerçek wrapper (alert-suppress). Wrapper marker'dan RESULT türetip
# cron_outcomes'a yazar; CANARY_SUPPRESS_ALERT=1 alert/event/notify'ı atlar.
CANARY_SUPPRESS_ALERT=1 "$WRAP" "$OK_JOB" bash -c 'echo "OUTCOME: pass | canary known-good"' >/dev/null 2>&1
CANARY_SUPPRESS_ALERT=1 "$WRAP" "$BAD_JOB" bash -c 'echo "OUTCOME: fail | canary known-bad"' >/dev/null 2>&1

# cron_outcomes'ta beklenen satırlar oluştu mu?
got_ok="$(sqlite3 "$DB_PATH" "SELECT result FROM cron_outcomes WHERE job='$OK_JOB' ORDER BY id DESC LIMIT 1;" 2>/dev/null)"
got_bad="$(sqlite3 "$DB_PATH" "SELECT result FROM cron_outcomes WHERE job='$BAD_JOB' ORDER BY id DESC LIMIT 1;" 2>/dev/null)"

if [ "$got_ok" = "pass" ] && [ "$got_bad" = "fail" ]; then
    emit_outcome pass "alarm-yolu sağlam: known-good→pass + known-bad→fail cron_outcomes'a yazıldı"
else
    emit_outcome fail "alarm-yolu BOZUK: known-good='${got_ok:-YOK}' (pass bekle) / known-bad='${got_bad:-YOK}' (fail bekle) — OUTCOME→cron_outcomes pipeline kırık"
fi
exit 0
