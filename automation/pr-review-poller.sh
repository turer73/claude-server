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
  local bad; bad=$(printf '%s' "$rollup" | jq '[.[] | select((.conclusion // .state // "") | test("FAIL|ERROR|CANCELLED|TIMED_OUT"; "i"))] | length' 2>/dev/null || echo 1)
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

daily_count() {  # bugünkü spawn sayısı
  local today; today=$(date -u +%Y-%m-%d)
  jq -r --arg d "$today" '.[$d] // 0' "$DAILY_FILE" 2>/dev/null || echo 0
}
daily_inc() {
  local today; today=$(date -u +%Y-%m-%d) tmp; tmp=$(mktemp)
  [ -f "$DAILY_FILE" ] || echo '{}' > "$DAILY_FILE"
  jq --arg d "$today" '.[$d]=((.[$d]//0)+1) | with_entries(select(.key>=($d|sub("-[0-9]+$";"")) or true))' "$DAILY_FILE" > "$tmp" 2>/dev/null && mv "$tmp" "$DAILY_FILE"
}

# FAZ2 trigger: main/master-hedef VE (insan-flag VEYA diff>eşik VEYA Codex-sessiz).
# Codex-sessiz: inline=0 (auto-skip); pilotta tetikleyici, ileride @codex-review-force-first.
faz2_trigger() {
  local repo="$1" num="$2" base="$3" adds="$4" dels="$5" labels="$6"
  case "$base" in main|master) ;; *) return 1 ;; esac
  echo "$labels" | grep -qi "$FLAG_LABEL" && return 0
  [ "$(( ${adds:-0} + ${dels:-0} ))" -gt "$DIFF_THRESHOLD" ] && return 0
  local cx; cx=$(gh api "repos/$repo/pulls/$num/comments" --jq '[.[]|select(.user.login=="chatgpt-codex-connector[bot]")]|length' 2>/dev/null || echo X)
  # cx sayı DEĞİLSE (gh-fail/404) -> codex-silent SAYMA (spurious-trigger yok).
  [[ "$cx" =~ ^[0-9]+$ ]] || return 1
  [ "$cx" -eq 0 ] && return 0   # Codex-sessiz (inline=0)
  return 1
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
    if ! faz2_trigger "$repo" "$num" "$base" "$adds" "$dels" "$labels"; then continue; fi
    CANDIDATES=$((CANDIDATES + 1))
    log "ADAY(FAZ2): $key HEAD=${head:0:8} base=$base diff=$((adds+dels)) labels=[$labels] \"$title\""
    # ── caps: per-run (MAX) + günlük (DAILY_MAX, Max-x20 paylaşımlı bütçe koruması) ──
    if [ "$REVIEWED" -ge "$MAX" ]; then log "  per-run cap ($MAX) -> kuyrukta (defer)"; continue; fi
    dc=$(daily_count)
    if [ "${dc:-0}" -ge "$DAILY_MAX" ]; then log "  günlük cap ($DAILY_MAX, bugün=$dc) -> defer (SESSIZ-DROP yok)"; continue; fi
    if [ "$DRY_RUN" = "1" ] || [ "$ENABLED" != "1" ]; then
      log "  [dry-run] spawn ATLANDI (gercek icin DRY_RUN=0 + PR_REVIEW_ENABLED=1 + spawn SPAWN_ENABLED=1)"
      continue
    fi
    # ── ENABLED: dedicated review-spawn (serialize: 1 spawn/sefer, döngü zaten sıralı) ──
    log "  [ENABLED] spawn: $SPAWN $repo $num"
    if SPAWN_ENABLED=1 bash "$SPAWN" "$repo" "$num" >>"$LOG_FILE" 2>&1; then
      mark_reviewed "$key" "$head"
      daily_inc
      REVIEWED=$((REVIEWED + 1))
      log "  spawn OK -> reviewed, mark_reviewed($key)"
    else
      log "  spawn FAIL $key (mark YOK -> sonraki run tekrar dener)"
    fi
  done
done
# OUTCOME marker (FAZ1 outcome-contract): fetch-fail varsa SESSIZ-pass DEGIL -> partial
# (en az bir repo taranamadi; aday gizlenmis olabilir).
if [ "${FETCH_FAIL:-0}" = "1" ]; then
  echo "OUTCOME: partial | fetch-fail (bir+ repo taranamadi) aday=$CANDIDATES reviewed=$REVIEWED"
else
  echo "OUTCOME: pass | aday=$CANDIDATES reviewed=$REVIEWED dry_run=$DRY_RUN"
fi
log "=== PR-review poll DONE: aday=$CANDIDATES reviewed=$REVIEWED fetch_fail=$FETCH_FAIL ==="
