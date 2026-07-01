#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# Panola ERP — Demo Reset + E2E Test Runner
# Klipper test sunucusunda her gece çalışır
#
# İşlevler:
#   1. Demo veritabanını resetle (seed data)
#   2. E2E testlerini çalıştır (Playwright)
#   3. Sonuçları raporla (Telegram + JSON)
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

# Konfigürasyon
PANOLA_DIR="/data/projects/panola"
RESULTS_DIR="/opt/linux-ai-server/logs/e2e"
# Playwright Ubuntu 26.04 (klipper host) destegi yok; resmi noble imaji kullanilir
# (e2e-live-test.sh ile ayni pattern). Imaj surumu panola'nin @playwright/test
# surumuyle birebir eslesmeli (browser revizyonu): 1.58.2 -> chromium 1208.
PLAYWRIGHT_IMAGE="mcr.microsoft.com/playwright:v1.58.2-noble"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
LOG_FILE="$RESULTS_DIR/e2e-$TIMESTAMP.log"

# Env
source /opt/linux-ai-server/.env 2>/dev/null || true
export E2E_EMAIL="${E2E_EMAIL:-demo@panola.app}"
export E2E_PASSWORD="${E2E_PASSWORD:?Set E2E_PASSWORD in .env}"
export E2E_BASE_URL="${E2E_BASE_URL:-https://panola.app}"
export E2E_SUPABASE_URL="${E2E_SUPABASE_URL:-${E2E_SUPABASE_URL:?Set E2E_SUPABASE_URL}}"
export E2E_SUPABASE_KEY="${E2E_SUPABASE_KEY:-}"
# New cloud seed (panola 4eb85d5) creates the demo user + RLS-bypass inserts via
# the service-role client; .env is bare (not auto-exported), so this must be
# exported explicitly or the seed gets an empty key and fail-louds.
export E2E_SUPABASE_SERVICE_ROLE_KEY="${E2E_SUPABASE_SERVICE_ROLE_KEY:?Set E2E_SUPABASE_SERVICE_ROLE_KEY in .env}"
export CI=true

# Dizin oluştur
mkdir -p "$RESULTS_DIR"

log() { echo "[$(date +%H:%M:%S)] $1" | tee -a "$LOG_FILE"; }

# ─── Outcome-contract (LIVESYS Faz 1) ───
# set -euo pipefail altinda mid-failure abort, son "mutlu satir"a ULASMAZ;
# bu yuzden OUTCOME'u EXIT-trap ile emit et (abort'ta bile gercek sonuc).
# Rubrik: seed-fail/0-gecti=fail, fail>0 ama gecen var=partial, hepsi-gecti=pass.
emit_outcome() {
  set +e
  local seed="${SEED_STATUS:-}" passed="${PASSED:-0}" failed="${FAILED:-0}" total="${TOTAL:-0}"
  local result detail
  if [ "$seed" != "✅" ]; then
    result="fail"; detail="seed basarisiz/ulasilmadi (seed='${seed:-abort}')"
  elif ! [ "$passed" -gt 0 ] 2>/dev/null; then
    result="fail"; detail="0 test gecti (passed=$passed total=$total)"
  elif [ "$failed" -gt 0 ] 2>/dev/null; then
    result="partial"; detail="$passed/$total ($failed fail)"
  else
    result="pass"; detail="$passed/$total"
  fi
  echo "OUTCOME: $result | $detail"
}
trap emit_outcome EXIT

# ─── Telegram Bildirim ───
send_telegram() {
  local msg="$1"
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    curl --max-time 15 --connect-timeout 5 -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      -d "chat_id=${TELEGRAM_CHAT_ID}" \
      -d "text=${msg}" \
      -d "parse_mode=HTML" > /dev/null 2>&1
  fi
}

log "═══ Panola ERP Demo Reset + E2E Test ═══"

# ─── 1. Seed Data Reset ───
log "🌱 Demo verisi resetleniyor..."
cd "$PANOLA_DIR"

if npx tsx e2e/seed-demo-data.ts >> "$LOG_FILE" 2>&1; then
  log "✅ Seed data başarılı"
  SEED_STATUS="✅"
else
  log "❌ Seed data hatası"
  SEED_STATUS="❌"
fi

# ─── 2. E2E Testleri ───
log "🧪 E2E testleri başlıyor..."

# Playwright --reporter=json JSON'u stdout'a yazar. Ayri bir dosyaya yonlendir,
# stderr LOG_FILE'a dussun. (Eski versiyon: >> LOG_FILE 2>&1 ile JSON log'a karisip
# e2e-results.json hic olusmuyordu → grep dosya yok → 0/0 sahte rapor.)
PLAYWRIGHT_RESULTS="$RESULTS_DIR/playwright-results-$TIMESTAMP.json"
PLAYWRIGHT_RC=0
# Resmi Playwright imaji icinde kos: browser'lar /ms-playwright'ta gomulu, host'a
# kurulum gerekmez (Ubuntu 26.04 destegi yok). JSON stdout -> host dosyasi,
# stderr -> LOG_FILE. --user + HOME=/tmp ile rapor host kullanicisi olarak yazilir.
docker run --rm \
  -v "$PANOLA_DIR:/work" \
  -w /work \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e CI=true \
  -e E2E_EMAIL="$E2E_EMAIL" \
  -e E2E_PASSWORD="$E2E_PASSWORD" \
  -e E2E_BASE_URL="$E2E_BASE_URL" \
  -e E2E_SUPABASE_URL="$E2E_SUPABASE_URL" \
  -e E2E_SUPABASE_KEY="$E2E_SUPABASE_KEY" \
  "$PLAYWRIGHT_IMAGE" \
  npx playwright test --reporter=json > "$PLAYWRIGHT_RESULTS" 2>>"$LOG_FILE" || PLAYWRIGHT_RC=$?

if [ "$PLAYWRIGHT_RC" -eq 0 ]; then
  log "✅ Tüm E2E testleri geçti"
  TEST_STATUS="✅ PASSED"
else
  log "❌ Bazı E2E testleri başarısız (playwright rc=$PLAYWRIGHT_RC)"
  TEST_STATUS="❌ FAILED"
fi

# JSON dosyasi var ve gecerli mi? Yoksa sahte 0/0 yerine gorunur fail.
if [ ! -s "$PLAYWRIGHT_RESULTS" ] || ! jq -e . "$PLAYWRIGHT_RESULTS" >/dev/null 2>&1; then
  log "⚠️ Playwright JSON ciktisi bos veya bozuk: $PLAYWRIGHT_RESULTS"
  TEST_STATUS="⚠️ UNKNOWN (JSON missing/invalid)"
  PASSED=0
  FAILED=0
  SKIPPED=0
else
  PASSED=$(jq -r '.stats.expected // 0' "$PLAYWRIGHT_RESULTS")
  FAILED=$(jq -r '.stats.unexpected // 0' "$PLAYWRIGHT_RESULTS")
  SKIPPED=$(jq -r '.stats.skipped // 0' "$PLAYWRIGHT_RESULTS")
fi

# ─── 3. Rapor ───
TOTAL=$((PASSED + FAILED + SKIPPED))
REPORT="<b>🧪 Panola ERP E2E Rapor</b>
<code>$(date '+%Y-%m-%d %H:%M')</code>

Seed: $SEED_STATUS
Test: $TEST_STATUS
Geçen: $PASSED / $TOTAL
Başarısız: $FAILED
Atlanan: $SKIPPED

Ortam: $E2E_BASE_URL"

send_telegram "$REPORT"
log "$REPORT"

# ─── 4. Temizlik ───
# 7 günden eski logları sil
find "$RESULTS_DIR" -name "e2e-*.log" -mtime +7 -delete 2>/dev/null

log "═══ Bitti ═══"
