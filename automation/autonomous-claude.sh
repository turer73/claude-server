#!/bin/bash
# autonomous-claude.sh — Two-tier autonomous note handler
#
# Tier 1 (Ollama, $0, ~3sn):
#   - Classifier qwen2.5:7b ile not'u 4 kategoriden birine atar
#   - ACK / ACTIONABLE / DISCUSSION / URGENT
#
# Tier 2 (routing):
#   - ACK         -> local handle: mark read + brief memory entry, NO Claude
#   - ACTIONABLE  -> Claude spawn (allowlist'li, --dangerously-skip YOK)
#   - DISCUSSION  -> defer: mark read YAPMA, kullanici sonra gorur
#   - URGENT      -> bilgi topla + memory + mark read YAPMA
#
# Tier 2 ACTIONABLE: Claude Max plan kapsam (OAuth), $0 marjinal.
# Ayri permission scope: autonomous-claude-settings.json (Bash deny: sudo,
# rm, docker, push, vps-run). Bypass YOK.
#
# Kullanim: autonomous-claude.sh <NOTE_ID> <FROM_DEVICE> "<TITLE>" "<PREVIEW>"

set -euo pipefail

LOG_FILE="${AUTONOMOUS_LOG:-/opt/linux-ai-server/data/hook-logs/autonomous-claude.log}"
LOCK_FILE="${AUTONOMOUS_LOCK:-/tmp/klipper-autonomous-claude.lock}"
THROTTLE_FILE="${AUTONOMOUS_THROTTLE:-/opt/linux-ai-server/data/hook-state/autonomous-last-spawn.txt}"
THROTTLE_MIN_SECONDS="${AUTONOMOUS_THROTTLE_S:-60}"
SETTINGS_FILE="${AUTONOMOUS_SETTINGS:-/opt/linux-ai-server/automation/autonomous-claude-settings.json}"
GUARDRAILS="${AUTONOMOUS_GUARDRAILS:-/opt/linux-ai-server/automation/autonomous-claude-guardrails.md}"
MODEL="${AUTONOMOUS_MODEL:-claude-sonnet-4-6}"
CLASSIFIER="${AUTONOMOUS_CLASSIFIER:-/opt/linux-ai-server/automation/autonomous-classifier.sh}"
DB="${HOOK_DB:-/opt/linux-ai-server/data/claude_memory.db}"
API_BASE="${HOOK_API:-http://127.0.0.1:8420/api/v1/memory}"
ENV_FILE="${HOOK_ENV_FILE:-/opt/linux-ai-server/.env}"

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$THROTTLE_FILE")" 2>/dev/null || true

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '[%s] %s\n' "$(ts)" "$*" >> "$LOG_FILE"; }

get_key() {
    grep '^MEMORY_API_KEY=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"' | head -c 200
}

if [ $# -lt 4 ]; then
    log "usage: $0 <NOTE_ID> <FROM> <TITLE> <PREVIEW>"
    exit 2
fi

NOTE_ID="$1"
FROM="$2"
TITLE="$3"
PREVIEW="$4"

# ---------- Throttle ----------
if [ -f "$THROTTLE_FILE" ]; then
    LAST_SPAWN=$(cat "$THROTTLE_FILE" 2>/dev/null || echo 0)
    NOW=$(date +%s)
    DELTA=$((NOW - LAST_SPAWN))
    if [ "$DELTA" -lt "$THROTTLE_MIN_SECONDS" ]; then
        log "throttled: note #$NOTE_ID skip (delta=${DELTA}s)"
        exit 0
    fi
fi

# ---------- Lock ----------
exec 9>"$LOCK_FILE"
if ! flock -n -w 10 9; then
    log "lock failed: another autonomous run active"
    exit 0
fi

# ---------- Interactive Claude detection ----------
if [ "${SKIP_INTERACTIVE_CHECK:-0}" != "1" ]; then
    INTERACTIVE_FOUND=0
    for pid in $(pgrep -u klipperos -x "claude" 2>/dev/null || true); do
        [ "$pid" = "$$" ] && continue
        cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || echo "")
        case "$cmdline" in
            *" -p "*|*"--print"*) ;;
            *) INTERACTIVE_FOUND=1; break ;;
        esac
    done
    if [ "$INTERACTIVE_FOUND" = "1" ]; then
        log "skip: interactive Claude session active (pid=$pid)"
        exit 0
    fi
fi

# ---------- Tam not içeriği ----------
FULL_CONTENT=$(sqlite3 "$DB" "SELECT content FROM notes WHERE id=$NOTE_ID" 2>/dev/null || echo "$PREVIEW")
[ -z "$FULL_CONTENT" ] && FULL_CONTENT="$PREVIEW"

# ---------- TIER 1: Ollama classifier ----------
log "classifying note #$NOTE_ID ..."
CLASSIFICATION=$(bash "$CLASSIFIER" "$NOTE_ID" "$TITLE" "$FULL_CONTENT" 2>>"$LOG_FILE" || echo "DISCUSSION")
log "note #$NOTE_ID classified as: $CLASSIFICATION"

# ---------- TIER 2: routing ----------
KEY=$(get_key)

handle_ack() {
    # Local handle: mark read + brief memory entry. No LLM.
    log "ACK route #$NOTE_ID — local handle (no Claude)"
    [ -z "$KEY" ] && { log "MEMORY_API_KEY missing, skip"; return 1; }

    # Mark read
    curl -fsS --max-time 5 -X PUT "$API_BASE/notes/$NOTE_ID/read" \
        -H "X-Memory-Key: $KEY" >/dev/null 2>&1 || log "mark read failed #$NOTE_ID"

    # Brief memory entry
    local now slug
    now=$(ts)
    slug="autonomous-ack-${NOTE_ID}-$(date -u +%Y%m%d-%H%M)"
    NOTE_ID_VAR="$NOTE_ID" FROM_VAR="$FROM" TITLE_VAR="$TITLE" SLUG_VAR="$slug" \
    python3 <<'PY'
import json, os, urllib.request
key = open('/opt/linux-ai-server/.env').read()
key = [l.split('=',1)[1].strip() for l in key.splitlines() if l.startswith('MEMORY_API_KEY=')][0]
body = json.dumps({
    'type': 'project',
    'name': os.environ['SLUG_VAR'],
    'description': f"Otonom ACK route — note #{os.environ['NOTE_ID_VAR']} from {os.environ['FROM_VAR']}: {os.environ['TITLE_VAR'][:80]}",
    'content': f"Note #{os.environ['NOTE_ID_VAR']} from {os.environ['FROM_VAR']} classified as ACK by qwen2.5:7b. Local handler marked read with no further action. Content was confirmatory/acknowledgment only.",
    'source_device': 'klipper-autonomous',
    'rationale': 'Autonomous mode ACK classification — local handle, no LLM spawn'
}, ensure_ascii=False).encode('utf-8')
req = urllib.request.Request('http://127.0.0.1:8420/api/v1/memory/memories',
    data=body, method='POST',
    headers={'Content-Type':'application/json; charset=utf-8','X-Memory-Key':key})
try:
    print(urllib.request.urlopen(req, timeout=5).read().decode())
except Exception as e:
    print(f'memory write error: {e}')
PY
}

handle_actionable() {
    log "ACTIONABLE route #$NOTE_ID — spawn Claude (Max plan, $MODEL, allowlist)"
    date +%s > "$THROTTLE_FILE"

    local prompt spawn_log
    prompt="Otonom modda spawn edildin. Yeni bir not geldi:

=== NOTE METADATA ===
ID: #$NOTE_ID
From: $FROM
Title: $TITLE
Classified as: ACTIONABLE (qwen2.5:7b classifier)

=== NOTE CONTENT ===
$FULL_CONTENT

=== TALIMAT ===
Bu note ACTIONABLE olarak siniflandirildi. Yapilmasi gereken somut bir is var.

Yapabilirsin (settings allowlist):
- Read/Edit/Write: /opt/linux-ai-server/** ve /home/klipperos/work/**
- Git local: status/diff/log/add/commit (push YOK, push kullanici onayi gerek)
- Test: npx tsc/eslint/vitest, ruff, pytest
- DB sorgu: sqlite3 (SELECT/INSERT/UPDATE notes ve memories)
- Internal API: curl 127.0.0.1:8420
- Note mark read sonunda

Yapamazsin (settings deny):
- sudo, systemctl, docker, ssh, scp, rsync
- rm, dd
- git push, git rebase, git reset --hard
- gh pr merge/close
- VPS prod (vps-run.sh)
- Web fetch/search

Akis:
1. Note'u oku, somut isi belirle
2. Gerekli dosyalari Read et
3. Edit/Write yap
4. Test komutlarini cag (tsc/eslint/vitest/ruff)
5. Test passlanirsa git add + git commit (push yapma)
6. Note'u okundu isaretle (curl PUT /notes/$NOTE_ID/read)
7. Kisa rapor yaz, cik

Kisa rapor formati:
Action: <yapildi/deferred-test-fail/deferred-out-of-scope>
Note ID: #$NOTE_ID
Commits: <hash hash hash>
Tests: <pass/fail>
Result: <bir-iki cumle>"

    spawn_log="${LOG_FILE%.log}-spawn-${NOTE_ID}-$(date +%s).log"

    set +e
    # NOT: --bare OAuth'u disable ediyor (Max plan auth fail).
    # Bunun yerine prompt'u + guardrails'i siki tutarak Claude'u tek-amaca
    # zorluyoruz. SessionStart hook'tan gelen dashboard context'i Claude
    # gormekle birlikte guardrails "sadece bu noteu isle, baska hicbir
    # seye dokunma" diyor.
    claude -p "$prompt" \
        --append-system-prompt "$(cat "$GUARDRAILS")" \
        --settings "$SETTINGS_FILE" \
        --output-format json \
        --model "$MODEL" \
        < /dev/null \
        > "$spawn_log" 2>&1
    local rc=$?
    set -e

    log "spawn complete: note #$NOTE_ID rc=$rc log=$spawn_log"
}

handle_discussion() {
    log "DISCUSSION route #$NOTE_ID — defer to user (mark read YAPILMADI)"
    # Memory entry: kullaniciya isaret
    NOTE_ID_VAR="$NOTE_ID" FROM_VAR="$FROM" TITLE_VAR="$TITLE" \
    python3 <<'PY'
import json, os, urllib.request
key = open('/opt/linux-ai-server/.env').read()
key = [l.split('=',1)[1].strip() for l in key.splitlines() if l.startswith('MEMORY_API_KEY=')][0]
body = json.dumps({
    'type': 'project',
    'name': f"autonomous-deferred-{os.environ['NOTE_ID_VAR']}",
    'description': f"Otonom DISCUSSION defer — note #{os.environ['NOTE_ID_VAR']} from {os.environ['FROM_VAR']} bekleniyor",
    'content': f"Note #{os.environ['NOTE_ID_VAR']} ({os.environ['FROM_VAR']}: {os.environ['TITLE_VAR'][:100]}) qwen2.5:7b ile DISCUSSION olarak siniflandirildi. Karar/gorus bekleniyor; otonom mod bu durumda mark read YAPMADI. Kullanici siradaki interactive oturumda gormeli.",
    'source_device': 'klipper-autonomous',
    'rationale': 'Autonomous mode DISCUSSION classification — defer to user'
}, ensure_ascii=False).encode('utf-8')
req = urllib.request.Request('http://127.0.0.1:8420/api/v1/memory/memories',
    data=body, method='POST',
    headers={'Content-Type':'application/json; charset=utf-8','X-Memory-Key':key})
try:
    urllib.request.urlopen(req, timeout=5).read()
except Exception as e:
    print(f'memory write error: {e}')
PY
}

handle_urgent() {
    log "URGENT route #$NOTE_ID — info gather + memory + mark read YAPILMADI"
    NOTE_ID_VAR="$NOTE_ID" FROM_VAR="$FROM" TITLE_VAR="$TITLE" CONTENT_VAR="$FULL_CONTENT" \
    python3 <<'PY'
import json, os, urllib.request
key = open('/opt/linux-ai-server/.env').read()
key = [l.split('=',1)[1].strip() for l in key.splitlines() if l.startswith('MEMORY_API_KEY=')][0]
body = json.dumps({
    'type': 'project',
    'name': f"autonomous-urgent-{os.environ['NOTE_ID_VAR']}",
    'description': f"!!! URGENT !!! note #{os.environ['NOTE_ID_VAR']} from {os.environ['FROM_VAR']}",
    'content': f"!!! URGENT !!!\n\nNote #{os.environ['NOTE_ID_VAR']} ({os.environ['FROM_VAR']}: {os.environ['TITLE_VAR'][:100]}) qwen2.5:7b ile URGENT olarak siniflandirildi. Otonom mod bilgi topladi ama harekete gecmedi — kullanici/insan onayi gerek.\n\nFull content:\n{os.environ['CONTENT_VAR'][:2000]}\n\nMark read YAPILMADI — kullanici siradaki oturumda hemen gormeli.",
    'source_device': 'klipper-autonomous',
    'rationale': 'Autonomous mode URGENT classification — alert and defer'
}, ensure_ascii=False).encode('utf-8')
req = urllib.request.Request('http://127.0.0.1:8420/api/v1/memory/memories',
    data=body, method='POST',
    headers={'Content-Type':'application/json; charset=utf-8','X-Memory-Key':key})
try:
    urllib.request.urlopen(req, timeout=5).read()
except Exception as e:
    print(f'memory write error: {e}')
PY
}

case "$CLASSIFICATION" in
    ACK)         handle_ack ;;
    ACTIONABLE)  handle_actionable ;;
    DISCUSSION)  handle_discussion ;;
    URGENT)      handle_urgent ;;
    *)           log "unknown classification: $CLASSIFICATION (default: discussion)"; handle_discussion ;;
esac

exit 0
