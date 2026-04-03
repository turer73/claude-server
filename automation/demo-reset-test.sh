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
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
LOG_FILE="$RESULTS_DIR/e2e-$TIMESTAMP.log"

# Env
source /opt/linux-ai-server/.env 2>/dev/null || true
export E2E_EMAIL="${E2E_EMAIL:-demo@panola.app}"
export E2E_PASSWORD="${E2E_PASSWORD:-Demo2026!xK9}"
export E2E_BASE_URL="${E2E_BASE_URL:-https://panola.app}"
export E2E_SUPABASE_URL="${E2E_SUPABASE_URL:-http://194.163.134.239:8080}"
export E2E_SUPABASE_KEY="${E2E_SUPABASE_KEY:-}"
export CI=true

# Dizin oluştur
mkdir -p "$RESULTS_DIR"

log() { echo "[$(date +%H:%M:%S)] $1" | tee -a "$LOG_FILE"; }

# ─── Telegram Bildirim ───
send_telegram() {
  local msg="$1"
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
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

if npx playwright test --reporter=json >> "$LOG_FILE" 2>&1; then
  log "✅ Tüm E2E testleri geçti"
  TEST_STATUS="✅ PASSED"
  PASSED=$(grep -c '"status": "passed"' e2e-results.json 2>/dev/null || echo "?")
  FAILED="0"
else
  log "❌ Bazı E2E testleri başarısız"
  TEST_STATUS="❌ FAILED"
  PASSED=$(grep -c '"status": "passed"' e2e-results.json 2>/dev/null || echo "?")
  FAILED=$(grep -c '"status": "failed"' e2e-results.json 2>/dev/null || echo "?")
fi

# ─── 3. Rapor ───
TOTAL=$((PASSED + FAILED))
REPORT="<b>🧪 Panola ERP E2E Rapor</b>
<code>$(date +%Y-%m-%d %H:%M)</code>

Seed: $SEED_STATUS
Test: $TEST_STATUS
Geçen: $PASSED / $TOTAL
Başarısız: $FAILED

Ortam: $E2E_BASE_URL"

send_telegram "$REPORT"
log "$REPORT"

# ─── 4. Temizlik ───
# 7 günden eski logları sil
find "$RESULTS_DIR" -name "e2e-*.log" -mtime +7 -delete 2>/dev/null

log "═══ Bitti ═══"
