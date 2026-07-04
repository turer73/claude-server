# _spawn-worktree-lib.sh — Spawn-isolation Faz-1 çekirdeği (source-edilir; kasıtlı 644).
# Tasarım: docs/spawn-isolation-design.md §2.2-2.5. autonomous-claude.sh source eder;
# testler İZOLE source eder (main-side-effect yok — bu yüzden lib).
#
# Sözleşme (çağıran sağlar, yoksa güvenli-default):
#   SPAWN_REPO_ROOT  ana-checkout (default /opt/linux-ai-server)
#   SPAWN_WT_BASE    worktree-havuzu (default /opt/linux-ai-server-worktrees)
#   SPAWN_ISOLATION  1=aktif (default), 0=eski shared-davranış (acil-rollback)
#   LOG_FILE + log() — yoksa stderr'e düşen minimal-log tanımlanır
#   TELEGRAM_ALERT   alert-script yolu (fallback-emit için; yoksa/koşamazsa sessiz-geçmez, log kalır)

SPAWN_REPO_ROOT="${SPAWN_REPO_ROOT:-/opt/linux-ai-server}"
SPAWN_WT_BASE="${SPAWN_WT_BASE:-/opt/linux-ai-server-worktrees}"
SPAWN_ISOLATION="${SPAWN_ISOLATION:-1}"
TELEGRAM_ALERT="${TELEGRAM_ALERT:-/opt/linux-ai-server/automation/telegram-alert.sh}"
LOG_FILE="${LOG_FILE:-/dev/stderr}"
if ! type log >/dev/null 2>&1; then
    log() { printf '[wt] %s\n' "$*" >> "$LOG_FILE"; }
fi

# Globaller: setup başarılıysa WT_PATH/WT_BASE_SHA/WT_NONCE dolu; boş = shared-fallback.
WT_PATH=""; WT_BASE_SHA=""; WT_NONCE=""; SPAWN_WORK_REF=""

_wt_fallback() {
    # §2.5: fail-open AMA sessiz-değil — CRITICAL log + Telegram (pollution-riski görünür).
    local note_id="$1" why="$2"
    WT_PATH=""; WT_BASE_SHA=""
    log "CRITICAL: spawn-isolation FALLBACK-SHARED note=#$note_id ($why) — /opt-pollution riski!"
    bash "$TELEGRAM_ALERT" --kind generic \
        --text "⚠️ spawn-isolation FALLBACK: note #$note_id shared-checkout'ta koşacak ($why)" \
        >>"$LOG_FILE" 2>&1 || true
}

setup_spawn_worktree() {
    local note_id="$1"
    WT_PATH=""; WT_BASE_SHA=""; SPAWN_WORK_REF=""
    [ "$SPAWN_ISOLATION" = "1" ] || { log "spawn-isolation KAPALI (env) — shared-checkout"; return 0; }
    WT_NONCE="$(date +%s)-$$"
    local target="$SPAWN_WT_BASE/spawn-${note_id}-${WT_NONCE}"
    mkdir -p "$SPAWN_WT_BASE" 2>>"$LOG_FILE" || { _wt_fallback "$note_id" "wt-base mkdir FAIL"; return 0; }
    # P2-c stale-repair: bu-note'un önceki (kesik-retry) worktree'lerini temizle.
    local st
    for st in "$SPAWN_WT_BASE/spawn-${note_id}-"*; do
        [ -e "$st" ] || continue
        git -C "$SPAWN_REPO_ROOT" worktree remove --force "$st" >>"$LOG_FILE" 2>&1 || rm -rf "$st" 2>>"$LOG_FILE"
    done
    git -C "$SPAWN_REPO_ROOT" worktree prune >>"$LOG_FILE" 2>&1 || true
    # detach @ mevcut-HEAD (origin-fetch'e bağımlı-değil; /opt-HEAD = deploy-edilen-gerçek)
    if ! git -C "$SPAWN_REPO_ROOT" worktree add --detach "$target" HEAD >>"$LOG_FILE" 2>&1; then
        git -C "$SPAWN_REPO_ROOT" worktree prune >>"$LOG_FILE" 2>&1 || true   # repair-retry (tek)
        if ! git -C "$SPAWN_REPO_ROOT" worktree add --detach "$target" HEAD >>"$LOG_FILE" 2>&1; then
            _wt_fallback "$note_id" "worktree add 2x FAIL"; return 0
        fi
    fi
    WT_PATH="$target"
    WT_BASE_SHA=$(git -C "$WT_PATH" rev-parse HEAD 2>>"$LOG_FILE")
    log "spawn-worktree hazır: $WT_PATH (base=${WT_BASE_SHA:0:9})"
}

preserve_and_cleanup_worktree() {
    # P1 commit-koruma: HEAD base'den ilerlediyse durable-ref'e kaydet, SONRA remove.
    local note_id="$1"
    [ -n "$WT_PATH" ] && [ -d "$WT_PATH" ] || return 0
    local head
    head=$(git -C "$WT_PATH" rev-parse HEAD 2>>"$LOG_FILE" || echo "")
    if [ -n "$head" ] && [ -n "$WT_BASE_SHA" ] && [ "$head" != "$WT_BASE_SHA" ]; then
        SPAWN_WORK_REF="refs/spawn-work/${note_id}-${WT_NONCE}"
        if git -C "$SPAWN_REPO_ROOT" update-ref "$SPAWN_WORK_REF" "$head" 2>>"$LOG_FILE"; then
            log "spawn-commit korundu: $SPAWN_WORK_REF (${head:0:9}, base=${WT_BASE_SHA:0:9})"
        else
            # Ref-yazımı FAIL: worktree'yi SİLME (work-loss-önleme, P1) — insan-müdahale iste.
            log "CRITICAL: spawn-work ref-yazımı FAIL note=#$note_id — worktree KORUNDU: $WT_PATH"
            return 0
        fi
    fi
    git -C "$SPAWN_REPO_ROOT" worktree remove --force "$WT_PATH" >>"$LOG_FILE" 2>&1 || rm -rf "$WT_PATH" 2>>"$LOG_FILE"
    git -C "$SPAWN_REPO_ROOT" worktree prune >>"$LOG_FILE" 2>&1 || true
    WT_PATH=""
}
