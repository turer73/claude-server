#!/bin/bash
# autonomous-spawn-audit.sh — Passive audit autonomous spawn commits (P0.5)
#
# Akis:
#   1. spawn-head-<note_id>.txt'den eski HEAD oku
#   2. git log OLD_HEAD..HEAD ile spawn'in yarattigi commit'leri bul
#   3. Suspicious pattern kontrolu:
#      - Buyuk diff (>500 line eklenme veya >100 line silme)
#      - Sensitive file yolu (.env, secret, credential, *.key)
#      - Secret pattern (TOKEN/KEY/PASSWORD = uzun-deger)
#   4. Suspicious tespit -> memory entry + Telegram alert
#   5. AUTO-REVERT YAPMAZ — kullanici karari (manuel review URL)
#
# Bu pasif audit, otomatik revert yapacak Repo 2 pattern'inin guvenli
# varyantidir. False positive auto-revert dogru commit'i siler — onun
# yerine sadece raporla, kullanici karar versin.
#
# Kullanim: autonomous-spawn-audit.sh <NOTE_ID>

set -uo pipefail

NOTE_ID="${1:-}"
if [ -z "$NOTE_ID" ]; then
    echo "Usage: $0 <NOTE_ID>" >&2
    exit 2
fi

REPO="${AUDIT_REPO:-/opt/linux-ai-server}"
SPAWN_HEAD_DIR="${SPAWN_HEAD_DIR:-/opt/linux-ai-server/data/hook-state}"
SPAWN_HEAD_FILE="${SPAWN_HEAD_DIR}/spawn-head-${NOTE_ID}.txt"
LOG_FILE="${AUDIT_LOG:-/opt/linux-ai-server/data/hook-logs/autonomous-claude.log}"

BIG_INSERT_THRESHOLD="${AUDIT_BIG_INSERT:-500}"
BIG_DELETE_THRESHOLD="${AUDIT_BIG_DELETE:-100}"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '[%s] audit: %s\n' "$(ts)" "$*" >> "$LOG_FILE"; }

if [ ! -f "$SPAWN_HEAD_FILE" ]; then
    log "no spawn-head file for #$NOTE_ID — skip"
    exit 0
fi

OLD_HEAD=$(cat "$SPAWN_HEAD_FILE" 2>/dev/null | tr -d '[:space:]')
NEW_HEAD=$(git -C "$REPO" rev-parse HEAD 2>/dev/null)

if [ -z "$OLD_HEAD" ] || [ -z "$NEW_HEAD" ]; then
    log "missing HEAD references for #$NOTE_ID"
    rm -f "$SPAWN_HEAD_FILE"
    exit 0
fi

if [ "$OLD_HEAD" = "$NEW_HEAD" ]; then
    log "no commits for #$NOTE_ID (HEAD unchanged)"
    rm -f "$SPAWN_HEAD_FILE"
    exit 0
fi

# Yeni commit hashleri (chronological - eski once)
COMMITS=$(git -C "$REPO" log --format="%H" --reverse "${OLD_HEAD}..HEAD" 2>/dev/null)
if [ -z "$COMMITS" ]; then
    log "no new commits #$NOTE_ID (range empty)"
    rm -f "$SPAWN_HEAD_FILE"
    exit 0
fi

COUNT=$(printf '%s\n' "$COMMITS" | grep -c .)
log "auditing #$NOTE_ID: $COUNT new commit(s) ${OLD_HEAD:0:8}..${NEW_HEAD:0:8}"

SUSPICIOUS=()
COMMITS_SUMMARY=""

while IFS= read -r commit; do
    [ -z "$commit" ] && continue
    SHORT=${commit:0:8}
    SUBJECT=$(git -C "$REPO" show --no-patch --format="%s" "$commit" 2>/dev/null | head -c 80)

    SHORTSTAT=$(git -C "$REPO" show --shortstat --format="" "$commit" 2>/dev/null)
    INS=$(echo "$SHORTSTAT" | grep -oE '[0-9]+ insertion' | grep -oE '[0-9]+' || echo 0)
    DEL=$(echo "$SHORTSTAT" | grep -oE '[0-9]+ deletion' | grep -oE '[0-9]+' || echo 0)
    FILES=$(echo "$SHORTSTAT" | grep -oE '[0-9]+ file' | grep -oE '[0-9]+' || echo 0)
    INS=${INS:-0}; DEL=${DEL:-0}; FILES=${FILES:-0}

    COMMITS_SUMMARY="${COMMITS_SUMMARY}- ${SHORT} ${SUBJECT} (+${INS}/-${DEL}, ${FILES} files)
"

    # Check 1: Buyuk diff
    if [ "$INS" -gt "$BIG_INSERT_THRESHOLD" ]; then
        SUSPICIOUS+=("$SHORT: big insertion (+$INS lines, $FILES files)")
    fi
    if [ "$DEL" -gt "$BIG_DELETE_THRESHOLD" ]; then
        SUSPICIOUS+=("$SHORT: big deletion (-$DEL lines)")
    fi

    # Check 2: Sensitive file paths
    SENSITIVE=$(git -C "$REPO" show --name-only --format="" "$commit" 2>/dev/null \
        | grep -iE '(^|/)\.env($|\.)|secret|credential|private.*key|\.pem$|\.key$' \
        | grep -v 'public.key' | head -3 | tr '\n' ' ')
    if [ -n "$SENSITIVE" ]; then
        SUSPICIOUS+=("$SHORT: touched sensitive file(s): $SENSITIVE")
    fi

    # Check 3: Secret-looking pattern in diff additions
    SECRET_HIT=$(git -C "$REPO" show "$commit" 2>/dev/null \
        | grep -E '^\+[^+].*(TOKEN|SECRET|API[_-]?KEY|PASSWORD|BOT_TOKEN)[^=]*=[[:space:]]*[A-Za-z0-9/_+\.\-]{20,}' \
        | head -1)
    if [ -n "$SECRET_HIT" ]; then
        SUSPICIOUS+=("$SHORT: possible secret in diff (TOKEN/KEY/PASSWORD pattern)")
    fi
done <<< "$COMMITS"

# Temiz?
if [ "${#SUSPICIOUS[@]}" -eq 0 ]; then
    log "audit clean #$NOTE_ID: $COUNT commit(s) reviewed"
    rm -f "$SPAWN_HEAD_FILE"
    exit 0
fi

# SUSPICIOUS -> memory + Telegram
log "audit SUSPICIOUS #$NOTE_ID: ${#SUSPICIOUS[@]} issue(s)"
SUSP_LIST=$(printf -- '- %s\n' "${SUSPICIOUS[@]}")

NOTE_ID_VAR="$NOTE_ID" SUSP_VAR="$SUSP_LIST" COMMITS_VAR="$COMMITS_SUMMARY" \
DATE_VAR="$(date -u +%Y%m%d-%H%M)" \
RANGE_VAR="${OLD_HEAD:0:8}..${NEW_HEAD:0:8}" \
python3 <<'PY' 2>>"$LOG_FILE" || true
import json, os, urllib.request
KEY = [l.split('=',1)[1].strip() for l in open('/opt/linux-ai-server/.env').read().splitlines() if l.startswith('MEMORY_API_KEY=')][0]
body = json.dumps({
    'type': 'project',
    'name': f"autonomous-audit-suspicious-{os.environ['NOTE_ID_VAR']}-{os.environ['DATE_VAR']}",
    'description': f"Autonomous spawn audit SUSPICIOUS — note #{os.environ['NOTE_ID_VAR']} ({os.environ['RANGE_VAR']})",
    'content': f"## Autonomous spawn audit FAIL — manuel inceleme gerek\n\n**Note:** #{os.environ['NOTE_ID_VAR']}\n**Commit range:** {os.environ['RANGE_VAR']}\n\n## Suspicious issues ({len(os.environ['SUSP_VAR'].splitlines())})\n{os.environ['SUSP_VAR']}\n\n## All commits in range\n{os.environ['COMMITS_VAR']}\n\n## Aksiyon (manuel)\n```bash\n# Detayli incele:\ngit log {os.environ['RANGE_VAR']} --stat\ngit show <commit-hash>\n\n# Geri al (manuel karar — auto-revert YOK):\ngit revert <commit-hash>\n\n# Kabul ediyorsan bu memory entry'i archive et\n```\n\n**ONEMLI:** Audit pasif — otomatik revert yapilmadi. Suspicious pattern false positive olabilir, mutlaka incele.",
    'source_device': 'klipper-autonomous',
    'rationale': 'Autonomous spawn audit detected suspicious pattern — manuel review required, no auto-revert'
}, ensure_ascii=False).encode('utf-8')
req = urllib.request.Request('http://127.0.0.1:8420/api/v1/memory/memories',
    data=body, method='POST',
    headers={'Content-Type':'application/json; charset=utf-8','X-Memory-Key':KEY})
try: urllib.request.urlopen(req, timeout=5).read()
except Exception as e: print(f'audit memory err: {e}')
PY

# Telegram (HTML escape)
SUSP_ESC=$(printf '%s\n' "${SUSPICIOUS[@]}" | python3 -c 'import sys,html; sys.stdout.write(html.escape(sys.stdin.read()))' 2>/dev/null || printf '%s\n' "${SUSPICIOUS[@]}")
TG_MSG="<b>⚠ Autonomous Spawn Audit — SUSPICIOUS</b>

<b>Note:</b> #${NOTE_ID}
<b>Range:</b> ${OLD_HEAD:0:8}..${NEW_HEAD:0:8}
<b>Issues:</b> ${#SUSPICIOUS[@]}

<b>Detay:</b>
<pre>${SUSP_ESC}</pre>

<i>Incele:</i> <code>git log ${OLD_HEAD:0:8}..${NEW_HEAD:0:8} --stat</code>
<i>Auto-revert YOK — manuel inceleme gerek.</i>"

bash /opt/linux-ai-server/automation/telegram-alert.sh --kind generic --text "$TG_MSG" >> "$LOG_FILE" 2>&1 || \
    log "audit telegram alert failed for #$NOTE_ID"

rm -f "$SPAWN_HEAD_FILE"
exit 0
