#!/usr/bin/env bash
# ──────────────────────────────────────────────────
# Cron wrapper for test runner
# Crontab: 0 6 * * * /opt/linux-ai-server/automation/test-runner.sh
# ──────────────────────────────────────────────────

export PATH="/usr/local/bin:/usr/bin:/bin:/home/klipperos/.local/bin:/home/klipperos/.npm-global/bin"
export HOME="/home/klipperos"

# .env'den oku
if [ -f /opt/linux-ai-server/.env ]; then
  set -a; source /opt/linux-ai-server/.env; set +a
fi

# Telegram bildirim (doğrudan API — n8n'e gerek yok)
export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
export TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"

# n8n webhook fallback
export TEST_WEBHOOK_URL="${TEST_WEBHOOK_URL:-}"

# Pazartesi günleri başarılı sonuçları da bildir (haftalık özet)
if [ "$(date +%u)" = "1" ]; then
  export FORCE_NOTIFY=1
fi

# Log dosyası
LOG_DIR="/opt/linux-ai-server/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/test-runner-$(date +%Y%m%d).log"

# Eski logları temizle (30 günden eski)
find "$LOG_DIR" -name "test-runner-*.log" -mtime +30 -delete 2>/dev/null || true
# Eski sonuç dosyalarını temizle (7 günden eski)
find /tmp -name "test-results-*.json" -mtime +7 -delete 2>/dev/null || true

# ─── Outcome-contract (LIVESYS Faz 1) ───
# run-all-tests.sh sonucu coverage.db.test_runs'a yazar. OUTCOME'u oradan turet.
# RUN_START: bu run'in BASLANGIC zamani. En son test_runs satiri RUN_START'tan
# SONRA olmali — yoksa bu run yazamadan coktu (or. 3h icinde tekrar-calisip
# abort) ve ONCEKI run'in satirini "pass" sanmak SESSIZ-BASARI olur (Codex P1).
# Bu-run'a baglamak icin timestamp > RUN_START kontrolu (3h penceresinden siki).
# EXIT-trap -> abort'ta bile emit. Marker, "{ } >> LOG_FILE" redirect'i DISINDA
# gercek stdout'a gider (klipper-cron-wrap onu yakalar).
emit_test_outcome() {
  set +e
  local DB="/opt/linux-ai-server/data/coverage.db"
  local row passed failed this_run
  row=$(sqlite3 "$DB" "SELECT COALESCE(total_passed,0)||'|'||COALESCE(total_failed,0) FROM test_runs ORDER BY id DESC LIMIT 1;" 2>/dev/null)
  # En son satir BU run'da mi yazildi? (timestamp > run baslangici)
  this_run=$(sqlite3 "$DB" "SELECT CASE WHEN datetime((SELECT timestamp FROM test_runs ORDER BY id DESC LIMIT 1)) >= datetime('${RUN_START:-now}') THEN 1 ELSE 0 END;" 2>/dev/null)
  passed="${row%%|*}"; failed="${row##*|}"
  if [ -z "$row" ] || [ "$this_run" != "1" ]; then
    echo "OUTCOME: fail | bu-run test_runs satiri yazmadi (run cokmus/abort — onceki satir guvenilmez)"
  elif ! [ "${passed:-0}" -gt 0 ] 2>/dev/null; then
    echo "OUTCOME: fail | 0 test gecti (passed=$passed)"
  elif [ "${failed:-0}" -gt 0 ] 2>/dev/null; then
    echo "OUTCOME: partial | passed=$passed failed=$failed"
  else
    echo "OUTCOME: pass | passed=$passed"
  fi
}
# Run baslangic zamani — emit_test_outcome bunu kullanir (bu-run satiri ayirt).
RUN_START="$(date -Iseconds)"
trap emit_test_outcome EXIT

# Çalıştır ve logla
{
  echo "=== Test Runner $(date) ==="
  bash /opt/linux-ai-server/scripts/run-all-tests.sh
  echo "=== Bitti $(date) ==="
} >> "$LOG_FILE" 2>&1
