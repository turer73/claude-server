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
# Guard (disc#1256): HOOK_DEVICE SQL'e interpolasyonlu (WHERE to_device='$HOOK_DEVICE').
# Precedent: NOTE_ID (autonomous-claude.sh:93) + DAYS (gate-telemetry-report.sh:14) regex-validate.
# Somuru dusuk (env-var, servis-kullanicisi) ama daemon calisir-halde tutulur -> exit degil default.
if ! [[ "$HOOK_DEVICE" =~ ^[A-Za-z0-9_-]+$ ]]; then
    echo "note-poller: gecersiz HOOK_DEVICE='$HOOK_DEVICE' (yalniz [A-Za-z0-9_-]) -> 'klipper'e dusuluyor" >&2
    HOOK_DEVICE="klipper"
fi
PENDING_FILE="${PENDING_FILE:-/opt/linux-ai-server/data/hook-state/pending-notes.json}"
STATE_FILE="${STATE_FILE:-/opt/linux-ai-server/data/hook-state/poller-state.json}"
LOG_FILE="${LOG_FILE:-/opt/linux-ai-server/data/hook-logs/note-poller.log}"
POLL_INTERVAL="${POLL_INTERVAL:-30}"
PHASE_C_PYTHON="${PHASE_C_PYTHON:-/opt/linux-ai-server/venv/bin/python}"

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
    # Guard (disc#1256): last_seen state-file'dan; 'AND id > $last_seen' + printf %s'e girer.
    # Bozuk/tamperli JSON'da sayisal-olmayan deger SQL'e sizmasin -> 0'a dus (tum-unread yakalanir).
    [[ "$last_seen" =~ ^[0-9]+$ ]] || last_seen=0

    # Per-poll heartbeat (LIVESYS Faz2): liveness = processor-canli, yeni-not'tan
    # BAGIMSIZ. Idle poll'da bile last_poll_at tazelenir; yoksa liveness-monitor
    # poller-state mtime'ini activity sanip idle'i "olu" raporlar (B-FP). last_seen
    # korunur (yeni-not islenince asagida line ~173 spawned_max_id ile guncellenir).
    printf '{"last_seen_id": %s, "last_poll_at": "%s"}\n' "$last_seen" "$(ts)" > "$STATE_FILE"

    # Policy-gate #1222: held dispatch OTONOM-SPAWN'a GITMEZ (poller = otonom-isleme tetikleyicisi;
    # held burada sizarsa pending_notes.json'a girer -> autonomous-claude spawn eder = HOLD ETKISIZ,
    # #1222'nin tam onlemek istedigi "otonom-consequential-dispatch insan-gate'siz islenir" senaryosu).
    # Kolon-guard: status yoksa (fresh/merge-oncesi DB) filtre-yok (geri-uyum).
    local status_filter=""
    if [ "$(sqlite3 "$HOOK_DB" "SELECT COUNT(*) FROM pragma_table_info('notes') WHERE name='status';" 2>/dev/null || echo 0)" -gt 0 ]; then
        status_filter="AND COALESCE(status,'active')='active'"
    fi

    # Klipper-targeted veya broadcast unread notlari
    local new_notes
    new_notes=$(sqlite3 -json "$HOOK_DB" "
        SELECT id, from_device, to_device, title, substr(content, 1, 500) AS preview, created_at
        FROM notes
        WHERE (to_device='$HOOK_DEVICE' OR to_device IS NULL)
          AND read=0
          AND id > $last_seen
          $status_filter
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
        # Faz-A SS5 kill-switch + SS10 audit (docs/autonomous-comms-design.md): karar-mantigi
        # bagimsiz/test-edilebilir modulde (embedded-heredoc bash-quote-escaping kirilganligindan
        # kacinmak + pytest'ten import edilebilmek icin, bkz automation/note_poller_decide.py).
        if [ "${AUTONOMOUS_COMMS_PHASE_C:-0}" = "1" ]; then
            # Faz-C fail-safe pipeline: process spawn ETMEZ. Shadow varsayilan;
            # active send ancak env flip + taze insan onayi + metrik esikleriyle.
            if [ ! -x "$PHASE_C_PYTHON" ]; then
                log "phase-c interpreter unavailable: $PHASE_C_PYTHON"
                spawned_max_id=$last_seen
            else
                spawned_max_id=$(printf '%s' "$new_notes" | "$PHASE_C_PYTHON" /opt/linux-ai-server/automation/autonomous_comms_poller.py "$HOOK_DB" "$HOOK_DEVICE" "$last_seen" 2>>"$LOG_FILE" || echo $last_seen)
            fi
        else
            spawned_max_id=$(printf '%s' "$new_notes" | python3 /opt/linux-ai-server/automation/note_poller_decide.py "$HOOK_DB" "$HOOK_DEVICE" "$last_seen" 2>>"$LOG_FILE" || echo $last_seen)
        fi
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
