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

# Çalıştır ve logla
{
  echo "=== Test Runner $(date) ==="
  bash /opt/linux-ai-server/scripts/run-all-tests.sh
  echo "=== Bitti $(date) ==="
} >> "$LOG_FILE" 2>&1
