#!/usr/bin/env bash
# ──────────────────────────────────────────────────
# Haftalık dependency audit + Telegram bildirim
# Crontab: 0 9 * * 1 /opt/linux-ai-server/automation/weekly-audit.sh
# ──────────────────────────────────────────────────

export PATH="/usr/local/bin:/usr/bin:/bin:/home/klipperos/.local/bin"
export HOME="/home/klipperos"

# .env'den oku
if [ -f /opt/linux-ai-server/.env ]; then
  set -a; source /opt/linux-ai-server/.env; set +a
fi

TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

declare -A AUDITS
TOTAL_VULNS=0
REPORT=""

audit_project() {
  local name="$1"
  local dir="$2"

  if [ ! -f "$dir/package-lock.json" ]; then
    return
  fi

  local audit_out high=0 critical=0
  audit_out=$(cd "$dir" && npm audit --json --omit=dev 2>/dev/null) || true
  high=$(echo "$audit_out" | grep -oP '"high":\s*\K\d+' | head -1 || echo 0)
  critical=$(echo "$audit_out" | grep -oP '"critical":\s*\K\d+' | head -1 || echo 0)
  local total=$(( ${high:-0} + ${critical:-0} ))
  TOTAL_VULNS=$((TOTAL_VULNS + total))

  if [ "$total" -gt 0 ]; then
    REPORT+="⚠️ *$name*: ${critical} critical, ${high} high
"
    log "  ⚠ $name — $critical critical, $high high"
  else
    log "  ✓ $name — temiz"
  fi
}

log "═══ Haftalık Dependency Audit ═══"

audit_project "panola" "/data/projects/panola"
audit_project "bilge-arena" "/data/projects/bilge-arena"
audit_project "renderhane" "/data/projects/renderhane"
audit_project "petvet-web" "/data/projects/petvet/web"
audit_project "demo-saas" "/data/projects/demo-saas"

log "Toplam zafiyet: $TOTAL_VULNS"

# Telegram bildirim
if [ "$TOTAL_VULNS" -gt 0 ] && [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
  MSG="🔒 *Haftalık Dependency Audit*

$REPORT
Toplam: $TOTAL_VULNS yüksek/kritik zafiyet
🕐 $(date '+%d/%m/%Y')"

  curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d chat_id="$TELEGRAM_CHAT_ID" \
    -d parse_mode="Markdown" \
    -d text="$MSG" > /dev/null 2>&1 || true
  log "Telegram bildirim gönderildi"
fi
