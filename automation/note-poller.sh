#!/bin/bash
# note-poller.sh — Klipper-side note polling daemon
#
# Surer'in note_poller.ps1 pattern'ine paralel. Her POLL_INTERVAL saniyede
# bir SQLite DB'yi kontrol eder; yeni unread klipper notu varsa
# pending_notes.json'a yazar ve (opsiyonel) Telegram/desktop notification
# fire eder. Hooks (UserPromptSubmit, Stop) pending dosyasini okuyup
# context-injection yapar.
#
# Calisma modu: systemd service olarak surekli daemon. Veya manuel test
# icin tek-shot: `note-poller.sh --once`.
#
# Cikarim: daemon Claude oturumunu kendisi BASLATAMAZ (Claude Code agent
# user prompt'a gore calisir). Daemon "yeni not geldi" sinyalini saglar;
# kullanici prompt'unda veya turn-end Stop hook'unda surfaced edilir.
# Bu surer'in tasariminin birebir karsiligi.

set -euo pipefail

HOOK_DB="${HOOK_DB:-/opt/linux-ai-server/data/claude_memory.db}"
HOOK_DEVICE="${HOOK_DEVICE:-klipper}"
PENDING_FILE="${PENDING_FILE:-/opt/linux-ai-server/data/hook-state/pending-notes.json}"
STATE_FILE="${STATE_FILE:-/opt/linux-ai-server/data/hook-state/poller-state.json}"
LOG_FILE="${LOG_FILE:-/opt/linux-ai-server/data/hook-logs/note-poller.log}"
POLL_INTERVAL="${POLL_INTERVAL:-30}"

mkdir -p "$(dirname "$PENDING_FILE")" "$(dirname "$LOG_FILE")" 2>/dev/null || true

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '[%s] %s\n' "$(ts)" "$*" >> "$LOG_FILE"; }

rotate_log() {
    # 100 KB uzerine cikinca tail 200 satira indir
    if [ -f "$LOG_FILE" ] && [ "$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)" -gt 102400 ]; then
        tail -200 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
        log "log rotated"
    fi
}

bootstrap_state() {
    if [ ! -f "$STATE_FILE" ]; then
        # Baslangic 0: ilk poll'da TUM mevcut unread notlari yakalar.
        # (Eger sadece bundan sonrakileri yakalamak istersen, max id'yi
        # SELECT MAX(id) FROM notes ile bul ve buraya yaz.)
        printf '{"last_seen_id": 0, "bootstrapped_at": "%s"}\n' "$(ts)" > "$STATE_FILE"
        log "bootstrap: last_seen_id=0 (will catch all existing unread)"
    fi
}

poll_once() {
    local last_seen
    last_seen=$(python3 -c "import json; print(json.load(open('$STATE_FILE')).get('last_seen_id', 0))" 2>/dev/null || echo 0)

    # Klipper-targeted veya broadcast unread notlari
    local new_notes
    new_notes=$(sqlite3 -json "$HOOK_DB" "
        SELECT id, from_device, to_device, title, substr(content, 1, 500) AS preview, created_at
        FROM notes
        WHERE (to_device='$HOOK_DEVICE' OR to_device IS NULL)
          AND read=0
          AND id > $last_seen
        ORDER BY id
    " 2>/dev/null || echo '[]')

    if [ -z "$new_notes" ] || [ "$new_notes" = "[]" ]; then
        return 0
    fi

    local count
    count=$(printf '%s' "$new_notes" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)

    if [ "$count" -eq 0 ]; then
        return 0
    fi

    # Merge to pending_notes.json (existing pending'leri koru, yenileri ekle)
    PENDING_FILE="$PENDING_FILE" NEW_NOTES="$new_notes" python3 <<'PY'
import json, os
from pathlib import Path
pending_path = Path(os.environ['PENDING_FILE'])
existing = []
if pending_path.exists():
    try:
        existing = json.loads(pending_path.read_text())
    except Exception:
        existing = []
new = json.loads(os.environ['NEW_NOTES'])
existing_ids = {n['id'] for n in existing}
for n in new:
    if n['id'] not in existing_ids:
        existing.append(n)
# Son 50 not tutulur (FIFO)
existing = existing[-50:]
pending_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
PY

    log "new notes: $count -> $PENDING_FILE (priority sort + rate limit pending)"

    # Otonom mod: yeni not basina autonomous-claude.sh spawn et
    # xAI x-algorithm pattern 7 (priority queue, Key #4) + 8 (source diversity inspired):
    #   - Title'da URGENT/ACIL/breach varsa priority 1000 (en once spawn -> ilk lock)
    #   - ACK only patterns sona (priority -100, throttle'a takilirsa kayip dusuk)
    #   - Ayni source'tan ardarda max 3 (4+ deferred -> next poll'da pick olur)
    #
    # State guncellemesi: SADECE spawned not ID'lerinin maxi. Deferred (rate-limit)
    # notlar bir sonraki poll'da yine new_notes listesine girer.
    local spawned_max_id
    spawned_max_id=$last_seen
    if [ "${AUTONOMOUS_MODE:-0}" = "1" ]; then
        spawned_max_id=$(printf '%s' "$new_notes" | python3 -c "
import json, sys, subprocess, os, re
notes = json.load(sys.stdin)

# Priority scoring (yuksek = once spawn)
def score(n):
    title = (n['title'] or '').upper()
    s = 0
    # URGENT keyword bonus
    if any(k in title for k in ('URGENT', 'ACIL', 'BREACH', 'KVKK', 'CVE', 'SALDIRI', 'INCIDENT')):
        s += 1000
    # Gorev paketi structure -> ACTIONABLE early
    if 'GOREV PAKETI' in title or 'gorev_paketi' in (n.get('preview') or '').lower():
        s += 500
    # ACK prefix -> low priority
    if title.startswith('ACK'):
        s -= 100
    # Recency bonus (newer first)
    s += n['id']  # id monotonic
    return -s  # ters cevir (sort asc -> high priority first)

# Source diversity rate limit: ayni source'tan max 3 spawn bu batch'te
sorted_notes = sorted(notes, key=score)
source_count = {}
spawned = []
deferred_rate_limit = []
for n in sorted_notes:
    src = n['from_device']
    cnt = source_count.get(src, 0)
    if cnt >= 3:
        deferred_rate_limit.append(n['id'])
        continue
    source_count[src] = cnt + 1
    spawned.append(n)

# Spawn (priority order)
for n in spawned:
    nid = n['id']
    frm = n['from_device']
    title = (n['title'] or '')[:200]
    preview = (n['preview'] or '')[:500]
    cmd = ['/opt/linux-ai-server/automation/autonomous-claude.sh',
           str(nid), frm, title, preview]
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                     stdout=open('/dev/null', 'w'),
                     stderr=subprocess.STDOUT, start_new_session=True)

# State guncellemesi: SADECE spawned not ID'lerinin maxi.
# Deferred'lar bir sonraki poll'da yine pickup'lanir.
import sys
if spawned:
    spawned_max = max(n['id'] for n in spawned)
else:
    spawned_max = $last_seen
sys.stderr.write(f'spawned (priority order): {[n[\"id\"] for n in spawned]} deferred: {deferred_rate_limit}\n')
print(spawned_max)
" 2>>"$LOG_FILE" || echo $last_seen)
    else
        # AUTONOMOUS_MODE=0: tum batch state'e gec
        spawned_max_id=$(printf '%s' "$new_notes" | python3 -c "import json,sys; d=json.load(sys.stdin); print(max(n['id'] for n in d) if d else 0)" 2>/dev/null || echo 0)
    fi

    # State guncelle
    printf '{"last_seen_id": %s, "last_poll_at": "%s"}\n' "$spawned_max_id" "$(ts)" > "$STATE_FILE"
    log "state updated: last_seen_id=$spawned_max_id"
}

run_daemon() {
    bootstrap_state
    log "daemon start (interval=${POLL_INTERVAL}s, device=$HOOK_DEVICE, db=$HOOK_DB)"
    trap 'log "daemon stop (signal)"; exit 0' INT TERM
    while true; do
        poll_once
        rotate_log
        sleep "$POLL_INTERVAL"
    done
}

case "${1:-daemon}" in
    --once|once)
        bootstrap_state
        poll_once
        echo "poll done; check $PENDING_FILE"
        ;;
    --daemon|daemon|"")
        run_daemon
        ;;
    --status|status)
        echo "STATE: $(cat "$STATE_FILE" 2>/dev/null || echo none)"
        echo "PENDING ($(wc -l < "$PENDING_FILE" 2>/dev/null || echo 0) lines):"
        head -20 "$PENDING_FILE" 2>/dev/null || echo "  (empty)"
        ;;
    --help|-h|help)
        echo "Usage: $(basename "$0") [daemon|once|status]"
        echo "Env: HOOK_DB HOOK_DEVICE PENDING_FILE STATE_FILE LOG_FILE POLL_INTERVAL"
        ;;
    *)
        echo "Unknown: $1; use --help" >&2
        exit 2
        ;;
esac
