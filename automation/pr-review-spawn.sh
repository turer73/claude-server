#!/bin/bash
# pr-review-spawn.sh <owner/repo> <pr_num> — FAZ2 dedicated PR-review spawn.
#
# headless `claude -p` ile PR diff'ini review eder + TEK ÖZET bot-etiketli comment
# post eder (gh pr comment). review-scoped settings (sadece oku + gh-comment;
# write/commit/push/merge YOK). Caller (poller) trigger/cap/enable kontrol eder.
#
# GÜVENLİK: SPAWN_ENABLED=1 (poller PR_REVIEW_ENABLED ile set eder) DEĞİLSE
# gerçek spawn YOK -> komutu loglar (dry). Pilot: ilk-N comment insan-spot-check.
set -uo pipefail

REPO="${1:?owner/repo}"
PR="${2:?pr_num}"
NAME="$(basename "$REPO")"
LOCAL="/data/projects/$NAME"
[ "$NAME" = "claude-server" ] && LOCAL="/opt/linux-ai-server"
SETTINGS="/opt/linux-ai-server/automation/pr-review-settings.json"
MODEL="${PR_REVIEW_MODEL:-claude-sonnet-4-6}"
LOG="${PR_REVIEW_SPAWN_LOG:-/opt/linux-ai-server/data/hook-logs/pr-review-spawn.log}"
SPAWN_ENABLED="${SPAWN_ENABLED:-0}"
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
log() { echo "[$(date -Iseconds)] $1" | tee -a "$LOG"; }

[ -d "$LOCAL" ] || { log "FAIL: local checkout yok: $LOCAL"; echo "OUTCOME: fail | $REPO#$PR checkout-yok"; exit 3; }

# Bot-etiketli, TEK özet-comment talimatlı review prompt (pilot: direct review,
# multi-agent /code-review fan-out DEĞİL -> Max-x20 bütçe-dostu + basit).
read -r -d '' PROMPT <<PEOF || true
Sen otomatik bir PR-review ajanisin (pilot). Gorev (SADECE bunlar; baska hicbir sey yapma):
1. Bu PR'in diff'ini incele: gh pr diff $PR -R $REPO
2. Diff'i correctness-bug acisindan review et (inverted-condition, off-by-one, null-deref, missing-await, falsy-zero, sessiz-hata-yutma, escape eksikligi, removed-guard). Degisen fonksiyonun degismeyen satirlari da kapsamda.
3. Bulgulari TEK bir ozet PR-comment olarak post et: gh pr comment $PR -R $REPO --body "..."
   Comment'in EN BASINA bu prefix'i koy (aynen):
   [otomatik review - klipper; FP olabilir, insan-dogrula]
   Sonra bulgulari kisa madde-listesi olarak yaz (dosya:satir + tek-cumle). Bulgu yoksa "Belirgin correctness-bulgu yok" yaz.
KISIT: Kod DEGISTIRME, commit/push/merge YAPMA, baska dosyaya dokunMA. Sadece oku + tek-comment. Bittiginde "OUTCOME: pass | reviewed $REPO#$PR" yaz.
PEOF

cd "$LOCAL" || { log "FAIL cd $LOCAL"; echo "OUTCOME: fail | cd-fail"; exit 3; }

if [ "$SPAWN_ENABLED" != "1" ]; then
  log "[disabled] SPAWN ATLANDI: $REPO#$PR (gercek review icin SPAWN_ENABLED=1). cwd=$LOCAL model=$MODEL"
  echo "OUTCOME: pass | dry $REPO#$PR (spawn disabled)"
  exit 0
fi

log "SPAWN: $REPO#$PR (cwd=$LOCAL model=$MODEL settings=review-scoped)"
claude -p "$PROMPT" --model "$MODEL" --settings "$SETTINGS" >>"$LOG" 2>&1
RC=$?
if [ "$RC" -eq 0 ]; then
  log "SPAWN OK: $REPO#$PR"
  echo "OUTCOME: pass | reviewed $REPO#$PR"
else
  log "SPAWN FAIL rc=$RC: $REPO#$PR"
  echo "OUTCOME: fail | spawn rc=$RC $REPO#$PR"
fi
