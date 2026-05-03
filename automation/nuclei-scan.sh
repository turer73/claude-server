#!/usr/bin/env bash
# Nuclei haftalik vulnerability scan — kendi domain'lerimiz icin.
# 12K+ CVE/misconfig template, severity medium+ filter, rate-limit WAF dostu.
# self-pentest tamamlayicisi (smoke check vs vulnerability template).
#
# Crontab: 0 4 * * 0 (Pazar 04:00 — self-pentest 03:00'tan 1h sonra)
# Manuel:  bash /opt/linux-ai-server/automation/nuclei-scan.sh [domain]
#
# Bulgular memory API discoveries'a yazilir (project+type+title idempotent dedup).
# Telegram alert: yeni-only (memory API duplicate dondurmuyorsa eklenmedi).

set -uo pipefail

ROOT="/opt/linux-ai-server"
DOMAINS_FILE="$ROOT/automation/self-pentest.domains"
LOG_ROOT="/var/log/nuclei"
TODAY="$(date +%Y-%m-%d)"
RUN_DIR="$LOG_ROOT/$TODAY"
sudo mkdir -p "$RUN_DIR" 2>/dev/null
sudo chown klipperos:klipperos "$LOG_ROOT" 2>/dev/null
sudo chown klipperos:klipperos "$RUN_DIR" 2>/dev/null

[ -f "$ROOT/.env" ] && { set -a; . "$ROOT/.env"; set +a; }

MEMORY_API="http://127.0.0.1:8420/api/v1/memory/discoveries"
USER_AGENT="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
SCAN_HEADER="X-Klipper-Selfscan: $(hostname)-$(date +%Y%m%d)"
SEVERITY="${NUCLEI_SEVERITY:-medium,high,critical}"
RATE_LIMIT="${NUCLEI_RL:-30}"
INTER_DOMAIN_SEC="${NUCLEI_INTER:-60}"
TIMEOUT="${NUCLEI_TIMEOUT:-10}"

log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }

send_telegram() {
  [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ] || return 0
  curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage"     -d chat_id="$TELEGRAM_CHAT_ID" -d parse_mode="Markdown" -d text="$1" >/dev/null 2>&1
}

post_discovery() {
  [ -n "${MEMORY_API_KEY:-}" ] || return 0
  curl -s -X POST "$MEMORY_API"     -H "X-Memory-Key: $MEMORY_API_KEY" -H "Content-Type: application/json"     -d "$1" >/dev/null 2>&1
}

scan_domain() {
  local domain="$1"
  local outfile="$RUN_DIR/${domain//[^a-z0-9.]/_}.jsonl"
  log "=== $domain ==="
  nuclei     -target "https://${domain}"     -severity "$SEVERITY"     -rate-limit "$RATE_LIMIT"     -timeout "$TIMEOUT"     -header "$SCAN_HEADER"     -H "User-Agent: $USER_AGENT"     -jsonl -o "$outfile"     -silent -duc -nm 2>/dev/null || true

  [ -s "$outfile" ] || { log "$domain: bulgu yok"; return 0; }

  local count
  count=$(wc -l < "$outfile")
  log "$domain: $count bulgu"

  local project="${domain%%.*}"
  while IFS= read -r line; do
    local tid sev iname matched title
    tid=$(echo "$line" | jq -r '.["template-id"] // empty')
    sev=$(echo "$line" | jq -r '.info.severity // "unknown"')
    iname=$(echo "$line" | jq -r '.info.name // "unknown"')
    matched=$(echo "$line" | jq -r '.["matched-at"] // empty')
    [ -z "$tid" ] && continue
    title="[${sev}] ${iname} (${tid})"

    # type must be one of bug/fix/learning/config/workaround/architecture/plan
    # — memory API validation rejects "vuln" with 422. Use "bug"; severity is
    # already reflected in the title prefix.
    body=$(jq -n       --arg device "klipper" --arg project "$project" --arg type "bug"       --arg title "$title"       --arg details "Domain: $domain | Template: $tid | Matched: $matched | Severity: $sev"       '{device_name: $device, project: $project, type: $type, title: $title, details: $details}')
    post_discovery "$body"
  done < "$outfile"
}

# Main
if [ "$#" -gt 0 ]; then
  scan_domain "$1"
else
  while IFS= read -r domain; do
    [[ "$domain" =~ ^# ]] && continue
    [[ -z "$domain" ]] && continue
    scan_domain "$domain"
    sleep "$INTER_DOMAIN_SEC"
  done < "$DOMAINS_FILE"
fi

total=$(cat "$RUN_DIR"/*.jsonl 2>/dev/null | wc -l)
log "Toplam: $total"
if [ "${total:-0}" -gt 0 ]; then
  send_telegram "Nuclei scan: $total finding $(date '+%Y-%m-%d')"
fi
