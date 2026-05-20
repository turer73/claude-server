#!/bin/bash
# autonomous-spawn-retry.sh — DLQ pending_retry processor (P0.2)
#
# Cron: */15 * * * *
# Akis:
#   1. flock (concurrent guard)
#   2. SELECT spawn_failures WHERE status='pending_retry' AND attempt_num<3
#      AND (last_retry_at IS NULL OR last_retry_at < now-15min)
#   3. Her not icin: claude binary respawn
#      - rc=0 -> archived + summarizer trigger
#      - rc!=0 -> attempt_num++, last_retry_at=now
#        attempt_num >= 3 -> status=poison + Telegram + memory entry
#   4. Inter-spawn 5sn sleep (binary nefes)
#
# Exit codes:
#   0 = OK (idle veya islendi)
#   1 = lock taken (skip)
#   2 = DB unavailable / spawn_failures table missing
#
# Env override:
#   POISON_THRESHOLD=3
#   INTER_SPAWN_SLEEP=5
#   TELEGRAM_DRY_RUN=1  (test icin)

set -uo pipefail   # NOT -e: tek not fail tum loop'u kesmesin

# Isolation: cron'dan klipperos olarak tetikleniyor; otonom Claude'u
# klipper-auto altinda calistirmaliyiz (note-poller systemd unit ile ayni
# guvenlik modeli). Self-drop-privs: eger klipperos olarak girilirse, sudo
# ile klipper-auto'ya re-exec et.
if [ "$(id -un)" = "klipperos" ] && [ -z "${RETRY_PRIVS_DROPPED:-}" ]; then
    exec sudo -n -u klipper-auto \
        env RETRY_PRIVS_DROPPED=1 \
            HOME=/home/klipper-auto \
            HOOK_ENV_FILE=/opt/linux-ai-server/.env.autonomous \
            TELEGRAM_ENV_FILE=/opt/linux-ai-server/.env.autonomous \
            AUTONOMOUS_LOCK=/opt/linux-ai-server/data/hook-state/klipper-autonomous-claude.lock \
            RETRY_LOCK=/opt/linux-ai-server/data/hook-state/klipper-autonomous-spawn-retry.lock \
        "$0" "$@"
fi

LOG_FILE="${RETRY_LOG:-/opt/linux-ai-server/data/hook-logs/autonomous-spawn-retry.log}"
LOCK_FILE="${RETRY_LOCK:-/opt/linux-ai-server/data/hook-state/klipper-autonomous-spawn-retry.lock}"
DB="${HOOK_DB:-/opt/linux-ai-server/data/claude_memory.db}"
SETTINGS_FILE="${AUTONOMOUS_SETTINGS:-/opt/linux-ai-server/automation/autonomous-claude-settings.json}"
GUARDRAILS="${AUTONOMOUS_GUARDRAILS:-/opt/linux-ai-server/automation/autonomous-claude-guardrails.md}"
MODEL="${AUTONOMOUS_MODEL:-claude-sonnet-4-6}"
POISON_THRESHOLD="${POISON_THRESHOLD:-3}"
INTER_SPAWN_SLEEP="${INTER_SPAWN_SLEEP:-5}"
HOOK_LOG_DIR="${HOOK_LOG_DIR:-/opt/linux-ai-server/data/hook-logs}"

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '[%s] %s\n' "$(ts)" "$*" >> "$LOG_FILE"; }

# ---------- Lock ----------
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "another retry process running — skip"
    exit 1
fi

# ---------- DB ready check ----------
if ! sqlite3 "$DB" "SELECT 1 FROM spawn_failures LIMIT 1" >/dev/null 2>&1; then
    log "spawn_failures table missing — run scripts/migrate-spawn-failures.sh"
    exit 2
fi

# ---------- Pending picks ----------
PENDING=$(sqlite3 -cmd ".timeout 5000" -json "$DB" "
    SELECT id, note_id, from_device, title, IFNULL(preview,'') AS preview, attempt_num
    FROM spawn_failures
    WHERE status='pending_retry'
      AND attempt_num < $POISON_THRESHOLD
      AND (last_retry_at IS NULL
           OR datetime(last_retry_at) < datetime('now','-15 minutes'))
    ORDER BY first_failed_at ASC
    LIMIT 20
" 2>>"$LOG_FILE" || echo '[]')

COUNT=$(printf '%s' "$PENDING" | python3 -c "import json,sys;
try: print(len(json.load(sys.stdin)))
except: print(0)" 2>/dev/null || echo 0)

log "retry tick: $COUNT pending rows"
[ "$COUNT" = "0" ] && exit 0

# ---------- Poison alert (Telegram + memory) ----------
dlq_poison_alert() {
    local note_id="$1" from="$2" title="$3" attempts="$4" rc="$5" err_tail="$6"

    # 1. Telegram alert (HTML) — P0.1 helper reuse
    local title_safe err_short msg
    title_safe=$(printf '%s' "$title" | python3 -c 'import sys,html; sys.stdout.write(html.escape(sys.stdin.read()[:120]))' 2>/dev/null || echo "$title")
    err_short=$(printf '%s' "$err_tail" | python3 -c 'import sys,html; sys.stdout.write(html.escape(sys.stdin.read()[-400:]))' 2>/dev/null || echo "")

    msg="<b>☠ DLQ POISON — Autonomous Claude Spawn</b>

<b>Note:</b> #${note_id}
<b>From:</b> ${from}
<b>Title:</b> ${title_safe}
<b>Attempts:</b> ${attempts} (poison threshold)
<b>Last exit code:</b> ${rc}

<b>Error tail:</b>
<pre>${err_short}</pre>

<i>Incele:</i> <code>curl -H \"X-Memory-Key: \$KEY\" http://127.0.0.1:8420/api/v1/memory/spawn-failures?status=poison</code>"

    bash /opt/linux-ai-server/automation/telegram-alert.sh --kind generic --text "$msg" >> "$LOG_FILE" 2>&1 || \
        log "telegram poison alert FAILED note=#$note_id"

    # 2. Memory entry — audit trail, dashboard'da gozuksun
    NOTE_ID_VAR="$note_id" TITLE_VAR="$title" FROM_VAR="$from" \
    ATTEMPTS_VAR="$attempts" RC_VAR="$rc" ERR_VAR="$err_tail" \
    DATE_VAR="$(date -u +%Y%m%d-%H%M)" \
    python3 <<'PY' 2>>"$LOG_FILE" || true
import json, os, urllib.request
KEY = [l.split('=',1)[1].strip() for l in open(os.environ.get('HOOK_ENV_FILE', '/opt/linux-ai-server/.env')).read().splitlines() if l.startswith('MEMORY_API_KEY=')][0]
body = json.dumps({
    'type': 'project',
    'name': f"autonomous-spawn-poison-{os.environ['NOTE_ID_VAR']}-{os.environ['DATE_VAR']}",
    'description': f"DLQ POISON — note #{os.environ['NOTE_ID_VAR']} from {os.environ['FROM_VAR']}: {os.environ['TITLE_VAR'][:80]}",
    'content': f"!!! DLQ POISON !!!\n\nNote #{os.environ['NOTE_ID_VAR']} ({os.environ['FROM_VAR']}: {os.environ['TITLE_VAR'][:120]}) autonomous Claude spawn {os.environ['ATTEMPTS_VAR']} kez fail oldu (rc={os.environ['RC_VAR']}). Manuel inceleme gerekli.\n\n## Error tail\n```\n{os.environ['ERR_VAR'][-2000:]}\n```\n\n## Aksiyon\n- API: GET /api/v1/memory/spawn-failures?status=poison\n- Manuel retry: POST /api/v1/memory/spawn-failures/<id>/retry\n- Telegram alert gonderildi.",
    'source_device': 'klipper-autonomous',
    'rationale': 'DLQ poison threshold — manuel intervention required'
}, ensure_ascii=False).encode('utf-8')
req = urllib.request.Request('http://127.0.0.1:8420/api/v1/memory/memories',
    data=body, method='POST',
    headers={'Content-Type':'application/json; charset=utf-8','X-Memory-Key':KEY})
try: urllib.request.urlopen(req, timeout=5).read()
except Exception as e: print(f'memory write err: {e}')
PY
}

# ---------- Retry one row ----------
retry_one() {
    local dlq_id="$1" note_id="$2" from="$3" title="$4" preview="$5" attempt="$6"

    log "retry: dlq_id=$dlq_id note=#$note_id attempt=$attempt"

    # Note varlik kontrolu (silinmisse orphaned)
    local full_content
    full_content=$(sqlite3 -cmd ".timeout 5000" "$DB" "SELECT content FROM notes WHERE id=$note_id" 2>/dev/null)
    if [ -z "$full_content" ]; then
        log "note #$note_id silinmis — orphaned"
        sqlite3 -cmd ".timeout 5000" "$DB" "UPDATE spawn_failures SET status='orphaned', archived_at=datetime('now') WHERE id=$dlq_id"
        return 0
    fi

    # Prompt rebuild (handle_actionable line 157-201 ile ayni; DRY violation kabul — scope korunma)
    local prompt spawn_log
    prompt="Otonom modda RETRY spawn edildin (DLQ attempt #$((attempt+1))/$POISON_THRESHOLD). Onceki spawn fail olmustu (rc!=0), simdi tekrar deniyoruz:

=== NOTE METADATA ===
ID: #$note_id
From: $from
Title: $title
Retry attempt: $((attempt+1)) of $POISON_THRESHOLD

=== NOTE CONTENT ===
$full_content

=== TALIMAT ===
Bu note ACTIONABLE olarak siniflandirildi (onceki classify). Yapilmasi gereken somut bir is var.

Yapabilirsin (settings allowlist):
- Read/Edit/Write: /opt/linux-ai-server/** ve /home/klipperos/work/**
- Git local: status/diff/log/add/commit (push YOK)
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
5. Test passlanirsa git add + git commit
6. Note'u okundu isaretle (curl PUT /notes/$note_id/read)
7. Kisa rapor yaz, cik

Kisa rapor formati:
Action: <yapildi/deferred-test-fail/deferred-out-of-scope>
Note ID: #$note_id
Commits: <hash hash hash>
Tests: <pass/fail>
Result: <bir-iki cumle>"

    spawn_log="${HOOK_LOG_DIR}/autonomous-claude-retry-spawn-${note_id}-$(date +%s).log"

    set +e
    claude -p "$prompt" \
        --append-system-prompt "$(cat "$GUARDRAILS")" \
        --settings "$SETTINGS_FILE" \
        --output-format json \
        --model "$MODEL" \
        < /dev/null \
        > "$spawn_log" 2>&1
    local rc=$?
    set -e

    log "retry spawn done: note=#$note_id rc=$rc log=$spawn_log"

    if [ "$rc" -eq 0 ]; then
        sqlite3 -cmd ".timeout 5000" "$DB" "UPDATE spawn_failures SET status='archived', archived_at=datetime('now'), last_retry_at=datetime('now') WHERE id=$dlq_id"
        if [ -f "$spawn_log" ]; then
            bash /opt/linux-ai-server/automation/autonomous-spawn-summarize.sh \
                "$note_id" "$spawn_log" >> "$LOG_FILE" 2>&1 &
        fi
        log "retry SUCCESS: note=#$note_id archived dlq_id=$dlq_id"
        return 0
    fi

    # Fail path
    local err_slug new_attempt
    err_slug=$(tail -c 1500 "$spawn_log" 2>/dev/null | tr -d '\000' | sed "s/'/''/g" || echo "")
    new_attempt=$((attempt + 1))

    # OAuth race detection: 401 marker spawn JSON output icinde
    if grep -q '"api_error_status":401' "$spawn_log" 2>/dev/null; then
        log "OAUTH 401 detected retry note=#$note_id attempt=$new_attempt — possible refresh race"
        set +e
        bash /opt/linux-ai-server/automation/telegram-alert.sh \
            --kind oauth_race \
            --note-id "$note_id" \
            --spawn-log "$spawn_log" \
            --attempt "$new_attempt" \
            >> "$LOG_FILE" 2>&1
        set -e
    fi

    if [ "$new_attempt" -ge "$POISON_THRESHOLD" ]; then
        sqlite3 -cmd ".timeout 5000" "$DB" "UPDATE spawn_failures SET attempt_num=$new_attempt, exit_code=$rc, error_log='$err_slug', spawn_log_path='$spawn_log', last_retry_at=datetime('now'), status='poison', poisoned_at=datetime('now') WHERE id=$dlq_id"
        log "POISON: note=#$note_id after $new_attempt attempts"
        dlq_poison_alert "$note_id" "$from" "$title" "$new_attempt" "$rc" "$err_slug"
    else
        sqlite3 -cmd ".timeout 5000" "$DB" "UPDATE spawn_failures SET attempt_num=$new_attempt, exit_code=$rc, error_log='$err_slug', spawn_log_path='$spawn_log', last_retry_at=datetime('now') WHERE id=$dlq_id"
        log "retry FAIL: note=#$note_id attempt=$new_attempt/$POISON_THRESHOLD"
    fi
}

# ---------- Loop ----------
printf '%s' "$PENDING" | python3 -c "
import json, sys
for row in json.load(sys.stdin):
    # Tab-separated to avoid pipe collision with content
    print('\t'.join([str(row['id']), str(row['note_id']), row['from_device'], row['title'], row.get('preview','') or '', str(row['attempt_num'])]))
" 2>>"$LOG_FILE" | while IFS=$'\t' read -r dlq_id note_id from title preview attempt; do
    retry_one "$dlq_id" "$note_id" "$from" "$title" "$preview" "$attempt"
    sleep "$INTER_SPAWN_SLEEP"
done

log "retry tick done"
exit 0
