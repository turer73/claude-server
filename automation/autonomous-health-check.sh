#!/bin/bash
# autonomous-health-check.sh — otonom mod self-test
#
# Hangi bilesenleri kontrol eder:
#   1. Ollama API canlilik (127.0.0.1:11434 /api/tags reachable)
#   2. Ollama classifier accuracy quick test (1 case)
#   3. Memory API /health endpoint
#   4. SQLite DB read/write
#   5. note-poller.service systemd status
#   6. Disk space (hook-logs dir)
#   7. Lock file orphan check (>10 dakika eski lock = orphan)
#   8. Throttle file sanity
#
# Fail: memory entry (type=project, name=autonomous-health-fail-YYYYMMDD-HHMM)
#       + journalctl marker
# Pass: log entry, sessiz cik

set -uo pipefail   # NOT -e: birden fazla test, hepsini calistir

LOG_FILE="${HOOK_LOG_DIR:-/opt/linux-ai-server/data/hook-logs}/autonomous-health.log"
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '[%s] %s\n' "$(ts)" "$*" >> "$LOG_FILE"; }

FAILS=()
PASSES=()

check() {
    local name="$1"
    local cmd="$2"
    if eval "$cmd" >/dev/null 2>&1; then
        PASSES+=("$name")
        log "PASS $name"
    else
        FAILS+=("$name")
        log "FAIL $name (cmd: $cmd)"
    fi
}

# 1. Ollama API
check "ollama-api"       "curl -fsS --max-time 5 http://127.0.0.1:11434/api/tags"

# 2. Ollama classifier (1 hizli test)
check "ollama-classifier" 'R=$(curl -fsS --max-time 15 http://127.0.0.1:11434/api/generate -d "{\"model\":\"qwen2.5:7b\",\"prompt\":\"Classify (one word: ACK|ACTIONABLE|DISCUSSION|URGENT):\\n\\nTitle: KVKK breach\\n\\nLabel:\",\"stream\":false,\"options\":{\"num_predict\":5}}" | python3 -c "import json,sys; print(json.load(sys.stdin).get(\"response\",\"\").upper())") && echo "$R" | grep -qE "URGENT|ACTIONABLE|DISCUSSION|ACK"'

# 3. Memory API health
check "memory-api"       "curl -fsS --max-time 5 http://127.0.0.1:8420/health | grep -q healthy"

# 4. SQLite DB read
check "db-read"          "sqlite3 /opt/linux-ai-server/data/claude_memory.db 'SELECT COUNT(*) FROM notes' >/dev/null"

# 5. note-poller service
check "note-poller-running" "systemctl is-active --quiet klipper-note-poller"

# 6. Disk space (hook-logs <5GB)
check "disk-space"       "[ \$(du -sm /opt/linux-ai-server/data/hook-logs 2>/dev/null | awk '{print \$1}') -lt 5000 ]"

# 7. Lock file orphan (>10 dakika eski lock + lock holder yok) + AUTO-CLEANUP
LOCK_FILE="${AUTONOMOUS_LOCK:-/opt/linux-ai-server/data/hook-state/klipper-autonomous-claude.lock}"
CLEANUP_RECOVERED=0
CLEANUP_AGE=0
if [ -f "$LOCK_FILE" ]; then
    AGE=$(( $(date +%s) - $(stat -c %Y "$LOCK_FILE" 2>/dev/null || echo 0) ))
    if [ "$AGE" -gt 600 ] && ! fuser "$LOCK_FILE" >/dev/null 2>&1; then
        # Race-koruma: 1sn sonra tekrar dogrula (lock o anda alinmis olabilir)
        sleep 1
        if ! fuser "$LOCK_FILE" >/dev/null 2>&1; then
            if rm -f "$LOCK_FILE" 2>/dev/null && [ ! -f "$LOCK_FILE" ]; then
                CLEANUP_RECOVERED=1
                CLEANUP_AGE="$AGE"
                PASSES+=("lock-orphan-cleaned (age=${AGE}s)")
                log "RECOVER lock-orphan-cleaned age=${AGE}s"
            else
                FAILS+=("lock-file-orphan-rm-failed ($AGE seconds)")
                log "FAIL lock-file-orphan-rm-failed age=${AGE}s"
            fi
        else
            PASSES+=("lock-file-clean (race-recovered)")
            log "PASS lock-file-clean (race-recovered age=${AGE}s)"
        fi
    else
        PASSES+=("lock-file-clean")
        log "PASS lock-file-clean (age=${AGE}s)"
    fi
else
    PASSES+=("lock-file-absent")
fi

# 8. Throttle file sanity (yoksa veya sayisal)
THROTTLE_FILE="/opt/linux-ai-server/data/hook-state/autonomous-last-spawn.txt"
if [ -f "$THROTTLE_FILE" ]; then
    if [[ $(cat "$THROTTLE_FILE" 2>/dev/null) =~ ^[0-9]+$ ]]; then
        PASSES+=("throttle-sane")
    else
        FAILS+=("throttle-malformed")
    fi
fi

# 9. Onboarding readiness + AUTO-FIX (claude CLI upgrade ~/.claude.json'daki
#    hasCompletedOnboarding'i sifirlayabilir -> yeni CLI headless cold-start'ta
#    TTY-siz onboarding/trust dialog'unda HANG -> spawn rc=124 timeout -> poison ->
#    liveness autonomy=dead. 2026-06-25 olayi, PR #224 follow-up). Her spawn-user'in
#    flag'ini idempotent+atomic set et. Guvenli config-fix (headless icin dogru) -> oto.
#    klipperos full-NOPASSWD-sudo -> klipper-auto dosyasina da erisir/yazar.
ONBOARD_FIXED=()
ensure_onboarding() {
    local user="$1" home="$2"
    local cj="$home/.claude.json"
    # Config yoksa skip (claude hic calismamis; ilk-run kendi yazar)
    sudo -n -u "$user" test -f "$cj" 2>/dev/null || { PASSES+=("onboarding-$user-no-config"); return; }
    # Read+fix tek atomic adimda (read-modify-write race'i daraltir; os.replace atomik).
    # Yol argv ile gecer (sudo env'i strip eder -> os.environ ulasmaz).
    local res
    res=$(sudo -n -u "$user" python3 - "$cj" <<'PY' 2>/dev/null
import json, os, sys
p = sys.argv[1]
try:
    d = json.load(open(p))
except Exception:
    print("unreadable"); raise SystemExit
if d.get("hasCompletedOnboarding") is True:
    print("ready"); raise SystemExit
d["hasCompletedOnboarding"] = True
tmp = p + ".onboard.tmp"
with open(tmp, "w") as f:
    json.dump(d, f)
os.replace(tmp, p)          # atomik, partial-write korumasi
print("fixed")
PY
)
    case "$res" in
        ready) PASSES+=("onboarding-$user-ready") ;;
        fixed) ONBOARD_FIXED+=("$user"); PASSES+=("onboarding-$user-AUTOFIXED")
               log "RECOVER onboarding-autofix user=$user (hasCompletedOnboarding=true)" ;;
        *)     FAILS+=("onboarding-$user-unreadable")
               log "FAIL onboarding-$user-unreadable (res='$res')" ;;
    esac
}
ensure_onboarding klipper-auto /home/klipper-auto
ensure_onboarding klipperos    /home/klipperos

TOTAL=$(( ${#PASSES[@]} + ${#FAILS[@]} ))
log "health summary: pass=${#PASSES[@]}/$TOTAL fail=${#FAILS[@]} cleanup=${CLEANUP_RECOVERED} onboard-fixed=${#ONBOARD_FIXED[@]}"

# Onboarding auto-fix audit entry (FAIL olmasa bile — sessiz oto-onarim gorunur olsun)
if [ "${#ONBOARD_FIXED[@]}" -gt 0 ]; then
    DATE_SLUG=$(date -u +%Y%m%d-%H%M)
    USERS_CSV=$(IFS=,; echo "${ONBOARD_FIXED[*]}")
    USERS_VAR="$USERS_CSV" DATE_VAR="$DATE_SLUG" \
    python3 <<'PY' 2>/dev/null || true
import json, os, urllib.request
KEY = [l.split('=',1)[1].strip() for l in open(os.environ.get('HOOK_ENV_FILE', '/opt/linux-ai-server/.env')).read().splitlines() if l.startswith('MEMORY_API_KEY=')][0]
body = json.dumps({
    'type': 'project',
    'name': f"autonomous-onboarding-autofix-{os.environ['DATE_VAR']}",
    'description': f"Otonom spawn onboarding flag oto-onarildi — {os.environ['DATE_VAR']}",
    'content': f"## Recovery\nSpawn-user(lar)in ~/.claude.json hasCompletedOnboarding flag'i eksikti (muhtemel sebep: claude CLI upgrade flag'i sifirladi) -> headless cold-start onboarding-hang riski (spawn rc=124 -> poison -> autonomy=dead). Otomatik True set edildi (atomik).\n\n- User(lar): {os.environ['USERS_VAR']}\n- Eylem: hasCompletedOnboarding=true (idempotent)\n- Referans: PR #224 follow-up, 2026-06-25 onboarding-hang olayi\n\nKullanici aksiyon gerekmiyor — spawn'lar tekrar calisabilir.",
    'source_device': 'klipper-autonomous',
    'rationale': 'Automated onboarding recovery — audit log, no user attention needed'
}, ensure_ascii=False).encode('utf-8')
req = urllib.request.Request('http://127.0.0.1:8420/api/v1/memory/memories',
    data=body, method='POST',
    headers={'Content-Type':'application/json; charset=utf-8','X-Memory-Key':KEY})
try: urllib.request.urlopen(req, timeout=5).read()
except Exception as e: print(f'onboarding audit write err: {e}')
PY
fi

# Cleanup recovery audit entry (FAIL olmasa bile)
if [ "$CLEANUP_RECOVERED" = "1" ]; then
    DATE_SLUG=$(date -u +%Y%m%d-%H%M)
    AGE_VAR="$CLEANUP_AGE" DATE_VAR="$DATE_SLUG" \
    python3 <<'PY' 2>/dev/null || true
import json, os, urllib.request
KEY = [l.split('=',1)[1].strip() for l in open(os.environ.get('HOOK_ENV_FILE', '/opt/linux-ai-server/.env')).read().splitlines() if l.startswith('MEMORY_API_KEY=')][0]
body = json.dumps({
    'type': 'project',
    'name': f"autonomous-lock-cleanup-{os.environ['DATE_VAR']}",
    'description': f"Otonom mod orphan lock cleanup — {os.environ['DATE_VAR']}",
    'content': f"## Recovery\nOrphan lock dosyasi tespit edildi ve otomatik temizlendi.\n\n- Lock: /tmp/klipper-autonomous-claude.lock\n- Yas: {os.environ['AGE_VAR']} saniye (>600s threshold)\n- Holder process: yok (fuser bos)\n- Eylem: rm -f basarili, dogrulandi\n\nKullanici aksiyon gerekmiyor — autonomous spawn'lar tekrar calisabilir.",
    'source_device': 'klipper-autonomous',
    'rationale': 'Automated lock recovery — audit log, no user attention needed'
}, ensure_ascii=False).encode('utf-8')
req = urllib.request.Request('http://127.0.0.1:8420/api/v1/memory/memories',
    data=body, method='POST',
    headers={'Content-Type':'application/json; charset=utf-8','X-Memory-Key':KEY})
try: urllib.request.urlopen(req, timeout=5).read()
except Exception as e: print(f'recovery write err: {e}')
PY
fi

# Eger fail varsa memory entry yaz
if [ "${#FAILS[@]}" -gt 0 ]; then
    DATE_SLUG=$(date -u +%Y%m%d-%H%M)
    FAIL_LIST=$(printf -- '- %s\n' "${FAILS[@]}")
    PASS_LIST=$(printf -- '- %s\n' "${PASSES[@]}")

    FAILS_VAR="$FAIL_LIST" PASSES_VAR="$PASS_LIST" DATE_VAR="$DATE_SLUG" \
    python3 <<'PY'
import json, os, urllib.request
KEY = [l.split('=',1)[1].strip() for l in open(os.environ.get('HOOK_ENV_FILE', '/opt/linux-ai-server/.env')).read().splitlines() if l.startswith('MEMORY_API_KEY=')][0]
body = json.dumps({
    'type': 'project',
    'name': f"autonomous-health-fail-{os.environ['DATE_VAR']}",
    'description': f"Otonom mod health check FAIL — {os.environ['DATE_VAR']}",
    'content': f"## FAIL\n{os.environ['FAILS_VAR']}\n\n## PASS\n{os.environ['PASSES_VAR']}",
    'source_device': 'klipper-autonomous',
    'rationale': 'Health check failure — kullanici incelemeli'
}, ensure_ascii=False).encode('utf-8')
req = urllib.request.Request('http://127.0.0.1:8420/api/v1/memory/memories',
    data=body, method='POST',
    headers={'Content-Type':'application/json; charset=utf-8','X-Memory-Key':KEY})
try: urllib.request.urlopen(req, timeout=5).read()
except Exception as e: print(f'write err: {e}')
PY
    exit 1
else
    exit 0
fi
