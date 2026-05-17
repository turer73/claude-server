#!/bin/bash
# autonomous-claude.sh — Headless Claude spawn wrapper
#
# Tetikleyen: note-poller.sh yeni unread klipper not algiladiginda invoke eder.
# Bu wrapper: lock + throttle + budget guard + guardrails + logging ile
# `claude -p` non-interactive session baslatir.
#
# Hedef: insan kullanici olmadan, klipper'in yeni notlari otonom isleyebilmesi.
# Kullanim: ./autonomous-claude.sh <NOTE_ID> <FROM_DEVICE> "<TITLE>" "<PREVIEW>"

set -euo pipefail

LOG_FILE="${AUTONOMOUS_LOG:-/opt/linux-ai-server/data/hook-logs/autonomous-claude.log}"
LOCK_FILE="${AUTONOMOUS_LOCK:-/tmp/klipper-autonomous-claude.lock}"
THROTTLE_FILE="${AUTONOMOUS_THROTTLE:-/opt/linux-ai-server/data/hook-state/autonomous-last-spawn.txt}"
THROTTLE_MIN_SECONDS="${AUTONOMOUS_THROTTLE_S:-60}"
MAX_BUDGET_USD="${AUTONOMOUS_BUDGET:-0.50}"
GUARDRAILS="${AUTONOMOUS_GUARDRAILS:-/opt/linux-ai-server/automation/autonomous-claude-guardrails.md}"
MODEL="${AUTONOMOUS_MODEL:-claude-haiku-4-5}"   # Default haiku for cost

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$THROTTLE_FILE")" 2>/dev/null || true

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '[%s] %s\n' "$(ts)" "$*" >> "$LOG_FILE"; }

if [ $# -lt 4 ]; then
    log "usage: $0 <NOTE_ID> <FROM> <TITLE> <PREVIEW>"
    echo "usage: $0 <NOTE_ID> <FROM> <TITLE> <PREVIEW>" >&2
    exit 2
fi

NOTE_ID="$1"
FROM="$2"
TITLE="$3"
PREVIEW="$4"

# 1) Throttle: son spawn'dan bu yana yeterli zaman geçti mi?
if [ -f "$THROTTLE_FILE" ]; then
    LAST_SPAWN=$(cat "$THROTTLE_FILE" 2>/dev/null || echo 0)
    NOW=$(date +%s)
    DELTA=$((NOW - LAST_SPAWN))
    if [ "$DELTA" -lt "$THROTTLE_MIN_SECONDS" ]; then
        log "throttled: note #$NOTE_ID skip (delta=${DELTA}s < ${THROTTLE_MIN_SECONDS}s)"
        exit 0
    fi
fi

# 2) Lock: concurrent spawn engelle (10sn timeout)
exec 9>"$LOCK_FILE"
if ! flock -n -w 10 9; then
    log "lock failed: another autonomous-claude running"
    exit 0
fi

# 3) User-interactive Claude session var mi? Varsa skip (conflict riski).
# SKIP_INTERACTIVE_CHECK=1 ile test/debug icin atlanabilir.
if [ "${SKIP_INTERACTIVE_CHECK:-0}" != "1" ]; then
    INTERACTIVE_FOUND=0
    for pid in $(pgrep -u klipperos -x "claude" 2>/dev/null || true); do
        [ "$pid" = "$$" ] && continue
        cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || echo "")
        # -p / --print flag varsa headless, conflict yok
        case "$cmdline" in
            *" -p "*|*"--print"*) ;;
            *) INTERACTIVE_FOUND=1; break ;;
        esac
    done
    if [ "$INTERACTIVE_FOUND" = "1" ]; then
        log "skip: interactive Claude session detected (pid=$pid)"
        exit 0
    fi
fi

# 4) Tam not icerigini DB'den cek (preview kisali olabilir)
FULL_CONTENT=$(sqlite3 /opt/linux-ai-server/data/claude_memory.db \
    "SELECT content FROM notes WHERE id=$NOTE_ID" 2>/dev/null || echo "$PREVIEW")
if [ -z "$FULL_CONTENT" ]; then
    FULL_CONTENT="$PREVIEW"
fi

# 5) Prompt olustur
PROMPT="Otonom modda spawn edildin. Yeni bir not geldi:

=== NOTE METADATA ===
ID: #$NOTE_ID
From: $FROM
Title: $TITLE

=== NOTE CONTENT ===
$FULL_CONTENT

=== TALIMAT ===
Guardrails dosyandaki kararlari uygula. Bu not için:
1. Once karar agacini calistir (actionable / discussion / acil-security?)
2. Actionable ise işi yap (commit/edit/test, VPS YOK).
3. Discussion/review ise mark read YAPMA, kullanici gormeli.
4. Acil security/KVKK ise bilgi topla + memory entry yaz, mark read YAPMA.
5. Sonunda kisa rapor uret ve cik.

Note okundu işaretlemek için (eğer ACTIONABLE + tamamlandi):
curl -X PUT http://127.0.0.1:8420/api/v1/memory/notes/$NOTE_ID/read -H \"X-Memory-Key: \$KEY\""

# 6) Spawn — log everything
log "spawn: note #$NOTE_ID from $FROM, model=$MODEL, budget=$MAX_BUDGET_USD USD"
SPAWN_LOG="${LOG_FILE%.log}-spawn-${NOTE_ID}-$(date +%s).log"
date +%s > "$THROTTLE_FILE"

# claude -p ile non-interactive cagri
# --append-system-prompt-file: guardrails
# --max-budget-usd: cost cap
# --output-format json: parse-friendly
# --dangerously-skip-permissions: insan onayi yok
# --model: haiku for cost (sonnet/opus icin AUTONOMOUS_MODEL override)
set +e
RESULT=$(claude -p "$PROMPT" \
    --append-system-prompt "$(cat "$GUARDRAILS")" \
    --max-budget-usd "$MAX_BUDGET_USD" \
    --output-format json \
    --model "$MODEL" \
    --dangerously-skip-permissions \
    2>&1)
RC=$?
set -e

echo "$RESULT" > "$SPAWN_LOG"
log "spawn complete: note #$NOTE_ID rc=$RC, log=$SPAWN_LOG"

# Lock release otomatik (exec 9 kapaninca)
exit "$RC"
