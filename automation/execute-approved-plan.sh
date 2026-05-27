#!/bin/bash
# execute-approved-plan.sh — Onaylanmis Planning Mode plan'ini execute eder.
#
# Kullanim:
#   execute-approved-plan.sh <NOTE_ID>            # onay + execute
#   execute-approved-plan.sh <NOTE_ID> reject     # red, arsivle, execute YOK
#
# Akis:
#   1. pending-plans/<NOTE_ID>.json oku (FROM, TITLE, PREVIEW)
#   2. approve: AUTONOMOUS_BYPASS_CLASSIFY=1 + PLANNING_MODE=0 ile
#      autonomous-claude.sh'i tekrar cagir -> dogrudan handle_actionable execute path
#   3. Sonuc: success ise archive/<NOTE_ID>.executed.<ts>.json, fail ise pending dosyasi kalir
#
# Exit codes:
#   0 = approved+executed VEYA rejected
#   2 = bad args
#   3 = pending plan dosyasi yok
#   other = autonomous-claude.sh rc

set -euo pipefail

PENDING_DIR="${PENDING_PLANS_DIR:-/opt/linux-ai-server/data/hook-state/pending-plans}"
ARCHIVE_DIR="$PENDING_DIR/archive"
LOG_FILE="${AUTONOMOUS_LOG:-/opt/linux-ai-server/data/hook-logs/autonomous-claude.log}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$ARCHIVE_DIR" "$(dirname "$LOG_FILE")" 2>/dev/null || true

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '[%s] execute-approved-plan: %s\n' "$(ts)" "$*" >> "$LOG_FILE"; }

if [ $# -lt 1 ]; then
    printf 'usage: %s <NOTE_ID> [reject]\n' "$0" >&2
    exit 2
fi

NOTE_ID="$1"
ACTION="${2:-approve}"

PENDING="$PENDING_DIR/${NOTE_ID}.json"
if [ ! -f "$PENDING" ]; then
    log "no pending plan: #$NOTE_ID at $PENDING"
    printf 'no pending plan for note #%s\n' "$NOTE_ID" >&2
    exit 3
fi

case "$ACTION" in
    approve|reject) ;;
    *) printf 'bad action: %s (approve|reject)\n' "$ACTION" >&2; exit 2 ;;
esac

if [ "$ACTION" = "reject" ]; then
    target="$ARCHIVE_DIR/${NOTE_ID}.rejected.$(date +%s).json"
    mv "$PENDING" "$target"
    log "rejected: #$NOTE_ID -> $target"
    printf 'rejected: #%s\n' "$NOTE_ID"
    exit 0
fi

# approve: pending'den metadata oku
FROM=$(PEND="$PENDING" python3 -c "import json,os; print(json.load(open(os.environ['PEND']))['from'])")
TITLE=$(PEND="$PENDING" python3 -c "import json,os; print(json.load(open(os.environ['PEND']))['title'])")
PREVIEW=$(PEND="$PENDING" python3 -c "import json,os; print(json.load(open(os.environ['PEND']))['preview'])")

log "approved: #$NOTE_ID — resuming autonomous-claude.sh (bypass classify, planning off)"

# Bypass + planning off ile execute yolu
set +e
AUTONOMOUS_BYPASS_CLASSIFY=1 PLANNING_MODE=0 \
    bash "$SCRIPT_DIR/autonomous-claude.sh" "$NOTE_ID" "$FROM" "$TITLE" "$PREVIEW"
rc=$?
set -e

if [ "$rc" -eq 0 ]; then
    target="$ARCHIVE_DIR/${NOTE_ID}.executed.$(date +%s).json"
    mv "$PENDING" "$target"
    log "executed OK: #$NOTE_ID -> $target"
    printf 'executed: #%s (archive: %s)\n' "$NOTE_ID" "$target"
else
    log "execute FAILED rc=$rc (pending file kalir: $PENDING)"
    printf 'FAILED rc=%d for #%s (pending plan dosyasi korundu)\n' "$rc" "$NOTE_ID" >&2
fi

exit $rc
