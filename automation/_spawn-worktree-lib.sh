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
    mkdir -p "$SPAWN_WT_BASE" 2>>"$LOG_FILE" || { _wt_fallback "$note_id" "wt-base mkdir FAIL (fix: install.sh pre-create / sudo mkdir+chown klipperos $SPAWN_WT_BASE — #100422)"; return 0; }
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
    # Codex-re3 P1-CRUX: settings worktree-DIŞINDA (pool-parent) → cleanup ayrı siler
    # (worktree-remove onu kapsamaz; nonce-adlı tekil dosya).
    [ -n "$WT_NONCE" ] && rm -f "$SPAWN_WT_BASE/.spawn-settings-${WT_NONCE}.json" 2>>"$LOG_FILE"
    WT_PATH=""
}

# ── Faz-2 (P2-a): per-spawn settings — allow'ları worktree'ye daralt + deny-hook ──
# Base-settings'teki Read/Edit/Write(//opt/...) izinleri worktree'ye map'lenir + PreToolUse
# write-guard-hook enjekte (çift-katman: permission-daraltma + deterministik-hook).
#
# Codex-re3 P1-CRUX (GÜVENLİK): settings WORKTREE-DIŞINDA üretilir (pool-parent
# $SPAWN_WT_BASE/.spawn-settings-<nonce>.json). Worktree-İÇİNDE olsaydı spawn kendi
# settings'ini modify edip write-guard-hook'unu KALDIRABİLİRDİ (tampering). Pool-parent
# spawn'ın izin-listesinde YOK (yalnız $WT_PATH/** + /tmp/**) → spawn oraya yazamaz.
# Codex-re3 P1-Read: Read(//opt/**) DE worktree'ye map'lenir (orijinali KORUNUR + worktree
# EKLENİR); worktree /opt-ALTINDA-DEĞİL (ayrı pool-dizini) → yoksa spawn kendi-dosyalarını okuyamazdı.
SPAWN_SETTINGS=""
WRITE_GUARD="${WRITE_GUARD:-/opt/linux-ai-server/automation/spawn-write-guard.sh}"

make_spawn_settings() {
    local base_settings="$1"
    SPAWN_SETTINGS=""
    [ -n "$WT_PATH" ] || return 0   # shared-fallback'te base-settings kalır (eski-davranış)
    # Codex P2-b: guard-script yoksa/okunamıyorsa settings ÜRETME (hook runtime'da patlar =
    # belirsiz-davranış/fail-open-riski) → FAIL → caller shared-fallback'e düşer (fail-closed).
    if [ ! -f "$WRITE_GUARD" ] || [ ! -r "$WRITE_GUARD" ]; then
        log "CRITICAL: write-guard bulunamadı/okunamıyor: $WRITE_GUARD — settings üretilmedi"
        return 1
    fi
    # P1-CRUX: WORKTREE-DIŞI (pool-parent) — spawn buraya yazamaz (tampering-önleme).
    local out="$SPAWN_WT_BASE/.spawn-settings-${WT_NONCE}.json"
    local wtrel="${WT_PATH#/}"
    if ! jq --arg wt "$WT_PATH" --arg wtrel "$wtrel" --arg guard "$WRITE_GUARD" '
        .permissions.allow |= map(
            if . == "Read(//opt/linux-ai-server/**)"  then "Read(//opt/linux-ai-server/**)", "Read(//"  + $wtrel + "/**)"
            elif . == "Edit(//opt/linux-ai-server/**)"  then "Edit(//"  + $wtrel + "/**)"
            elif . == "Write(//opt/linux-ai-server/**)" then "Write(//" + $wtrel + "/**)"
            else . end)
        | .hooks.PreToolUse = ((.hooks.PreToolUse // []) + [{
            matcher: "Edit|Write|MultiEdit|NotebookEdit",
            hooks: [{type: "command",
                     command: ("SPAWN_WT_PATH=" + ($wt | @sh) + " bash " + ($guard | @sh))}]
          }])
    ' "$base_settings" > "$out" 2>>"$LOG_FILE"; then
        # Settings-üretimi FAIL → izolasyonsuz-settings'le devam ETME riski yerine
        # worktree'yi bırakıp shared-fallback'e düş (CRITICAL-emit'li, §2.5-tutarlı).
        log "CRITICAL: per-spawn settings üretimi FAIL — base-settings + shared'a düşülüyor"
        rm -f "$out" 2>>"$LOG_FILE"
        SPAWN_SETTINGS=""
        return 1
    fi
    SPAWN_SETTINGS="$out"
    log "per-spawn settings hazır: $out (worktree-DIŞI; Read+Edit+Write→worktree + guard-hook)"
}
