#!/bin/bash
# pr-review-poller.sh — Cross-project otomatik PR-review orkestratoru (FAZ: review-sistem)
#
# 7 repo'da ACIK PR'lari tarar; CI-yesil + bu-HEAD'de-henuz-review-edilmemis
# olanlari ADAY olarak secer. Her aday icin (ENABLED modda) bir Claude code-review
# spawn'i tetikler -> lokal `/code-review high <PR> --comment` -> bulgular PR'a inline.
#
# GUVENLIK: DRY_RUN=1 (VARSAYILAN) -> sadece adaylari listeler/loglar, SPAWN/POST YOK.
# Otomatik prod-PR-comment yazmadan once dry-run ile dogrulanmali. ENABLED icin
# PR_REVIEW_ENABLED=1 set (+ spawn entegrasyonu ayri-dogrulanmis adimda eklenir).
#
# Idempotency: data/hook-state/pr-review-state.json {repo#pr: reviewed_head_sha}.
# HEAD degisirse yeniden-review; degismediyse atla. Rate-limit: PR_REVIEW_MAX (vars 5).
set -uo pipefail

ROOT="/opt/linux-ai-server"
STATE_FILE="${PR_REVIEW_STATE:-$ROOT/data/hook-state/pr-review-state.json}"
LOG_FILE="${PR_REVIEW_LOG:-$ROOT/data/hook-logs/pr-review-poller.log}"
DRY_RUN="${DRY_RUN:-1}"                       # VARSAYILAN dry-run (guvenli)
ENABLED="${PR_REVIEW_ENABLED:-0}"             # spawn+post icin acik (ayri dogrulama sonrasi)
MAX="${PR_REVIEW_MAX:-5}"                      # tek run'da max review (Claude-spawn maliyet siniri)

REPOS=(
  "turer73/claude-server"
  "turer73/panola"
  "turer73/kuafor"
  "turer73/petvet"
  "turer73/bilge-arena"
  "turer73/renderhane"
  "turer73/koken-akademi"
)

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$STATE_FILE")" 2>/dev/null || true
[ -f "$STATE_FILE" ] || echo '{}' > "$STATE_FILE"
log() { echo "[$(date -Iseconds)] $1" | tee -a "$LOG_FILE"; }

# state okuma/yazma (jq ile)
reviewed_head() { jq -r --arg k "$1" '.[$k] // ""' "$STATE_FILE" 2>/dev/null; }
mark_reviewed() {
  local tmp; tmp=$(mktemp)
  jq --arg k "$1" --arg v "$2" '.[$k]=$v' "$STATE_FILE" > "$tmp" 2>/dev/null && mv "$tmp" "$STATE_FILE"
}

# CI-yesil mi? statusCheckRollup'ta FAILURE/ERROR yoksa + en az 1 SUCCESS varsa yesil.
ci_green() {
  local rollup="$1"
  # ACTION_REQUIRED/STALE de "yeşil-değil" (premature-review önle — surer minor-2).
  local bad; bad=$(printf '%s' "$rollup" | jq '[.[] | select((.conclusion // .state // "") | test("FAIL|ERROR|CANCELLED|TIMED_OUT|ACTION_REQUIRED|STALE"; "i"))] | length' 2>/dev/null || echo 1)
  local ok;  ok=$(printf '%s' "$rollup" | jq '[.[] | select((.conclusion // .state // "") | test("SUCCESS|NEUTRAL|SKIPPED"; "i"))] | length' 2>/dev/null || echo 0)
  # pending: CheckRun .status VEYA legacy StatusContext .state ("PENDING"/"EXPECTED") — ikisi de.
  local pend; pend=$(printf '%s' "$rollup" | jq '[.[] | select(((.status // "") | test("IN_PROGRESS|QUEUED|PENDING"; "i")) or ((.state // "") | test("PENDING|EXPECTED"; "i")))] | length' 2>/dev/null || echo 0)
  [ "${bad:-1}" -eq 0 ] && [ "${pend:-0}" -eq 0 ] && [ "${ok:-0}" -gt 0 ]
}

# ── FAZ2 (koşullu auto-review spawn) config + helper ──
FAZ2_PILOT_REPOS="${FAZ2_PILOT_REPOS:-turer73/claude-server}"  # pilot: sadece claude-server
FLAG_LABEL="${PR_REVIEW_FLAG_LABEL:-review-please}"            # insan-flag etiketi
DIFF_THRESHOLD="${PR_REVIEW_DIFF_THRESHOLD:-400}"             # büyük-diff eşiği
DAILY_MAX="${PR_REVIEW_DAILY_MAX:-10}"                         # günlük hard-cap (Max-x20 paylaşımlı)
DAILY_FILE="${PR_REVIEW_DAILY_FILE:-/opt/linux-ai-server/data/hook-state/pr-review-daily.json}"
SPAWN="/opt/linux-ai-server/automation/pr-review-spawn.sh"
# FAZ4-S4: yüksek-blast spawn-sinyali (klipper-spesifik blast-radius)
BLAST_SH="/opt/linux-ai-server/scripts/blast-radius.sh"
BLAST_THRESHOLD="${PR_REVIEW_BLAST_THRESHOLD:-5}"             # >= N consumer -> yüksek-blast

daily_count() {  # bugünkü spawn sayısı
  local today; today=$(date -u +%Y-%m-%d)
  jq -r --arg d "$today" '.[$d] // 0' "$DAILY_FILE" 2>/dev/null || echo 0
}
daily_inc() {
  local today; today=$(date -u +%Y-%m-%d) tmp; tmp=$(mktemp)
  [ -f "$DAILY_FILE" ] || echo '{}' > "$DAILY_FILE"
  jq --arg d "$today" '.[$d]=((.[$d]//0)+1) | with_entries(select(.key>=($d|sub("-[0-9]+$";""))))' "$DAILY_FILE" > "$tmp" 2>/dev/null && mv "$tmp" "$DAILY_FILE"
}

# Codex durumu (surer #99737 thumbsup-clean refinement): auto-skip ≠ temiz.
#   none     = pulls/N/reviews'da codex-entry YOK -> auto-skip (#13 gibi) -> force gerek
#   findings = entry VAR + inline VAR -> Codex bulgu buldu (FAZ1-digest yüzeye çıkarır)
#   clean    = entry VAR + inline YOK (+👍) -> gerçekten temiz
#   unknown  = gh-fail -> spurious-trigger yok
codex_state() {
  local repo="$1" num="$2" head="$3" rev inl
  # Codex-P2: review/comment'leri SADECE current-HEAD'e göre say. GitHub
  # pulls/N/reviews ESKI-commit review'larini da dondurur (.commit_id) -> PR
  # yeni-commit alinca stale-review "clean/findings" sayilip force+spawn ATLANIR.
  # commit_id==head ile filtrele (head bos ise eski-davranis, tum-review).
  if [ -n "$head" ]; then
    rev=$(gh api "repos/$repo/pulls/$num/reviews" --jq --arg h "$head" '[.[]|select(.user.login=="chatgpt-codex-connector[bot]" and .commit_id==$h)]|length' 2>/dev/null || echo X)
  else
    rev=$(gh api "repos/$repo/pulls/$num/reviews" --jq '[.[]|select(.user.login=="chatgpt-codex-connector[bot]")]|length' 2>/dev/null || echo X)
  fi
  [[ "$rev" =~ ^[0-9]+$ ]] || { echo unknown; return; }
  [ "$rev" -eq 0 ] && { echo none; return; }
  if [ -n "$head" ]; then
    inl=$(gh api "repos/$repo/pulls/$num/comments" --jq --arg h "$head" '[.[]|select(.user.login=="chatgpt-codex-connector[bot]" and (.commit_id==$h or .original_commit_id==$h))]|length' 2>/dev/null || echo 0)
  else
    inl=$(gh api "repos/$repo/pulls/$num/comments" --jq '[.[]|select(.user.login=="chatgpt-codex-connector[bot]")]|length' 2>/dev/null || echo 0)
  fi
  { [[ "$inl" =~ ^[0-9]+$ ]] && [ "$inl" -gt 0 ]; } && { echo findings; return; }
  echo clean
}

# FAZ2 karar: spawn | force-codex | skip. main-hedef ZORUNLU. flag/diff>eşik ->
# spawn (non-codex). Yoksa codex_state: none->force-codex (önce @codex review
# ücretsiz-zorla), findings/clean->skip (Codex hallediyor), unknown->skip.
faz2_decision() {
  local repo="$1" num="$2" base="$3" adds="$4" dels="$5" labels="$6" head="$7"
  case "$base" in main|master) ;; *) echo skip; return ;; esac
  echo "$labels" | grep -qi "$FLAG_LABEL" && { echo spawn; return; }
  [ "$(( ${adds:-0} + ${dels:-0} ))" -gt "$DIFF_THRESHOLD" ] && { echo spawn; return; }
  case "$(codex_state "$repo" "$num" "$head")" in
    none) echo force-codex ;;
    *) echo skip ;;
  esac
}

# FAZ4-S4: yüksek-blast mı? echo 1/0. SADECE claude-server (blast-radius klipper-
# spesifik). read-only + FAIL-SAFE (gh/git/script timeout veya hata -> 0 = sinyal yok,
# review akışını ASLA bloklamaz). consumer-bullet (5-boşluk) sayar; >= eşik -> yüksek.
blast_high() {
  local repo="$1" num="$2"
  case "$repo" in */claude-server) ;; *) echo 0; return ;; esac
  [ -x "$BLAST_SH" ] || { echo 0; return; }
  local basesha range out n
  basesha=$(timeout 15 gh pr view "$num" -R "$repo" --json baseRefOid -q .baseRefOid 2>/dev/null || true)
  ( cd "$ROOT" && timeout 30 git fetch -q origin "pull/$num/head" 2>/dev/null ) || { echo 0; return; }
  range="${basesha:-origin/master}...FETCH_HEAD"
  out=$( cd "$ROOT" && timeout 30 "$BLAST_SH" --diff "$range" 2>/dev/null || true )
  # UNIQUE consumer say (Codex P2): ayni dosya farkli tablolar altinda tekrar
  # bullet'lanir -> sort -u ile tekille (duplike sismesi esigi yaniltmasin).
  n=$(printf '%s\n' "$out" | grep '^     - ' | sort -u | grep -c '^' || true)
  { [ "${n:-0}" -ge "$BLAST_THRESHOLD" ] && echo 1; } || echo 0
}

log "=== PR-review poll START (DRY_RUN=$DRY_RUN ENABLED=$ENABLED MAX=$MAX DAILY_MAX=$DAILY_MAX) ==="
CANDIDATES=0 REVIEWED=0 FETCH_FAIL=0
for repo in "${REPOS[@]}"; do
  # gh hatasi (auth-expiry / rate-limit / outage) "0 PR" gibi YUTULMAMALI -> sessiz
  # arizayi onle: fetch-fail'i ayri isaretle, OUTCOME=partial yap (rc=0 != tarandi).
  if ! prs=$(gh pr list -R "$repo" --state open --json number,headRefOid,title,statusCheckRollup,isDraft,baseRefName,additions,deletions,labels 2>>"$LOG_FILE"); then
    log "FETCH-FAIL: $repo (gh hatasi — taranAMADI, aday gizlenmis olabilir)"
    FETCH_FAIL=1
    continue
  fi
  cnt=$(printf '%s' "$prs" | jq 'length' 2>/dev/null || echo 0)
  [ "${cnt:-0}" -eq 0 ] && continue
  for i in $(seq 0 $((cnt - 1))); do
    pr=$(printf '%s' "$prs" | jq ".[$i]")
    num=$(printf '%s' "$pr" | jq -r '.number')
    head=$(printf '%s' "$pr" | jq -r '.headRefOid')
    draft=$(printf '%s' "$pr" | jq -r '.isDraft')
    title=$(printf '%s' "$pr" | jq -r '.title' | head -c 60)
    rollup=$(printf '%s' "$pr" | jq '.statusCheckRollup')
    [ "$draft" = "true" ] && continue
    if ! ci_green "$rollup"; then continue; fi
    key="${repo}#${num}"
    if [ "$(reviewed_head "$key")" = "$head" ]; then continue; fi  # bu HEAD review edildi
    # FAZ2 pilot: sadece pilot-repo'larda spawn-aday
    case " $FAZ2_PILOT_REPOS " in *" $repo "*) ;; *) continue ;; esac
    base=$(printf '%s' "$pr" | jq -r '.baseRefName')
    adds=$(printf '%s' "$pr" | jq -r '.additions // 0')
    dels=$(printf '%s' "$pr" | jq -r '.deletions // 0')
    labels=$(printf '%s' "$pr" | jq -r '[.labels[]?.name] | join(",")')
    decision=$(faz2_decision "$repo" "$num" "$base" "$adds" "$dels" "$labels" "$head")
    # FAZ4-S4: spawn-değilse ama yüksek-blast ise -> spawn'a yükselt (küçük-diff/
    # büyük-etki: kritik paylaşılan tabloya minik dokunuş diff-eşiğinin altında
    # kalsa da review tetikler). main-base gerekli (faz2 zaten kontrol etti).
    if [ "$decision" != "spawn" ]; then
      case "$base" in
        main | master)
          if [ "$(blast_high "$repo" "$num")" = "1" ]; then
            log "ADAY(FAZ4-S4): $key yüksek-blast (>=${BLAST_THRESHOLD} consumer) -> spawn (diff küçük olsa da)"
            decision=spawn
          fi
          ;;
      esac
    fi
    [ "$decision" = "skip" ] && continue
    CANDIDATES=$((CANDIDATES + 1))
    fkey="force:${key}"
    # ── force-codex (surer #99737): Codex-auto-skip -> önce @codex review ZORLA
    # (ücretsiz, once-per-HEAD idempotent), bu poll DEFER. Sonraki poll Codex hâlâ
    # yoksa spawn-eligible. @codex-comment outward-write -> sadece ENABLED'da. ──
    if [ "$decision" = "force-codex" ]; then
      if [ "$(reviewed_head "$fkey")" = "$head" ]; then
        decision=spawn  # bu HEAD'de zaten force edildi, Codex gelmedi -> Claude-spawn
      else
        log "ADAY(FAZ2): $key Codex-auto-skip -> @codex review FORCE (defer)"
        if [ "$DRY_RUN" = "1" ] || [ "$ENABLED" != "1" ]; then
          log "  [dry] @codex review force ATLANDI"
        elif gh pr comment "$num" -R "$repo" --body "@codex review" >>"$LOG_FILE" 2>&1; then
          mark_reviewed "$fkey" "$head"; log "  @codex review forced (mark $fkey)"
        else
          log "  @codex force FAIL -> sonraki run tekrar"
        fi
        continue
      fi
    fi
    # ── decision=spawn: cap + dedicated review-spawn ──
    log "ADAY(FAZ2-spawn): $key HEAD=${head:0:8} base=$base diff=$((adds+dels)) labels=[$labels] \"$title\""
    if [ "$REVIEWED" -ge "$MAX" ]; then log "  per-run cap ($MAX) -> defer"; continue; fi
    dc=$(daily_count)
    if [ "${dc:-0}" -ge "$DAILY_MAX" ]; then log "  günlük cap ($DAILY_MAX, bugün=$dc) -> defer (SESSIZ-DROP yok)"; continue; fi
    if [ "$DRY_RUN" = "1" ] || [ "$ENABLED" != "1" ]; then
      log "  [dry-run] spawn ATLANDI (DRY_RUN=0 + PR_REVIEW_ENABLED=1 + SPAWN_ENABLED=1 gerek)"
      continue
    fi
    log "  [ENABLED] spawn: $SPAWN $repo $num"
    if SPAWN_ENABLED=1 bash "$SPAWN" "$repo" "$num" >>"$LOG_FILE" 2>&1; then
      mark_reviewed "$key" "$head"; daily_inc; REVIEWED=$((REVIEWED + 1))
      log "  spawn OK -> reviewed, mark_reviewed($key)"
    else
      # back-off (surer ADD-1): spawn-fail (rate-limit dahil) -> mark YOK -> sonraki
      # cron-run'a defer (retry-burst YOK; öncelik: interaktif > otonom-review).
      SPAWN_FAILS=$((${SPAWN_FAILS:-0} + 1))
      log "  spawn FAIL $key -> defer next-run (back-off, mark YOK)"
    fi
  done
done
# OUTCOME marker (FAZ1 outcome-contract) + monitoring (surer ADD-2): aday/reviewed/
# gunluk-spawn/spawn-fail gorunur (contention SESSIZ degil). fetch-fail VEYA spawn-fail
# -> partial (sessiz-pass yok).
DC_NOW=$(daily_count)
if [ "${FETCH_FAIL:-0}" = "1" ] || [ "${SPAWN_FAILS:-0}" -gt 0 ]; then
  echo "OUTCOME: partial | aday=$CANDIDATES reviewed=$REVIEWED gunluk=$DC_NOW spawn_fail=${SPAWN_FAILS:-0} fetch_fail=${FETCH_FAIL:-0}"
else
  echo "OUTCOME: pass | aday=$CANDIDATES reviewed=$REVIEWED gunluk=$DC_NOW dry_run=$DRY_RUN"
fi
log "=== PR-review poll DONE: aday=$CANDIDATES reviewed=$REVIEWED gunluk=$DC_NOW spawn_fail=${SPAWN_FAILS:-0} fetch_fail=$FETCH_FAIL ==="
