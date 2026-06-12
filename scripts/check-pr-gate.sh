#!/bin/bash
# check-pr-gate.sh — Pre-merge tek-komut gate denetimi (elle/oturum-içi kullanım).
# Kullanım: scripts/check-pr-gate.sh <pr-no> [repo]   (repo varsayılan: turer73/claude-server)
#
# Merge-gate'in (CI-yeşil + Codex-kontrol + Codecov) doğru durumunu TEK kaynaktan
# (automation/pr-gate-lib.sh) okur — elle gh-poll bug'larını (yanlış login / review-vs-
# comment / stale-HEAD) önler. Çıkış: 0 = MERGE-OK, 1 = HENÜZ DEĞİL.
set -uo pipefail
# Lib'i kendi konumuna göre bul (worktree + /opt ikisinde de çalışır).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "$HERE/../automation/pr-gate-lib.sh"

PR="${1:-}"
REPO="${2:-turer73/claude-server}"
[ -n "$PR" ] || { echo "kullanım: check-pr-gate.sh <pr-no> [repo]" >&2; exit 2; }

J=$(gh pr view "$PR" -R "$REPO" --json number,title,headRefOid,state,mergeable,mergeStateStatus,statusCheckRollup 2>/dev/null) || {
  echo "❌ gh PR çekilemedi ($REPO#$PR)"; exit 2; }

STATE=$(printf '%s' "$J" | jq -r '.state')
TITLE=$(printf '%s' "$J" | jq -r '.title')
HEAD=$(printf '%s' "$J" | jq -r '.headRefOid')
MERGEABLE=$(printf '%s' "$J" | jq -r '.mergeable')
ROLLUP=$(printf '%s' "$J" | jq -c '.statusCheckRollup')

echo "── PR-GATE: $REPO#$PR ($STATE) ──"
echo "  $TITLE"
echo "  HEAD: ${HEAD:0:9} | mergeable: $MERGEABLE"

# 1) CI
if ci_green "$ROLLUP"; then
  CI_OK=1; echo "  ✅ CI: yeşil"
else
  CI_OK=0
  FAILING=$(printf '%s' "$ROLLUP" | jq -r '.[] | select((.conclusion // .state // "") | test("FAIL|ERROR|CANCELLED|TIMED_OUT|ACTION_REQUIRED|STALE";"i")) | (.name // .context // "?")' 2>/dev/null | paste -sd, -)
  PENDING=$(printf '%s' "$ROLLUP" | jq -r '.[] | select(((.status // "")|test("IN_PROGRESS|QUEUED|PENDING";"i")) or ((.state // "")|test("PENDING|EXPECTED";"i"))) | (.name // .context // "?")' 2>/dev/null | paste -sd, -)
  echo "  ❌ CI: yeşil değil${FAILING:+ | fail: $FAILING}${PENDING:+ | pending: $PENDING}"
fi

# 2) Codex
CX=$(codex_state "$REPO" "$PR" "$HEAD")
case "$CX" in
  clean)    echo "  ✅ Codex: temiz onay (bu HEAD'de bulgu yok; formal-review ya da HEAD-sha'lı issue-verdict)";;
  findings) echo "  ⚠️  Codex: BULGU var (bu HEAD'de inline yorum) — ele al:";
            gh api "repos/$REPO/pulls/$PR/comments" 2>/dev/null | jq -r --arg h "$HEAD" '.[]|select(.user.login=="chatgpt-codex-connector[bot]" and (.commit_id==$h or .original_commit_id==$h))|"     - [\(.path):\(.line)] \(.body[0:100])"' 2>/dev/null | head -10;;
  none)
    IV=$(codex_issue_verdict "$REPO" "$PR")
    if [ -n "$IV" ]; then
      echo "  ⚠️  Codex: issue-comment verdict VAR ama bu HEAD'e değil (stale ya da sha-okunamadı) — yeni inceleme tetikle:"
      echo "     \"$IV\""
    else
      echo "  ⏳ Codex: bu HEAD'i HENÜZ incelemedi — '@codex-review' ile tetikle ve bekle"
    fi
    ;;
  unknown)  echo "  ❓ Codex: durum okunamadı (gh hata) — elle bak";;
esac

# 3) Codecov (b)
COV=$(codecov_patch "$REPO" "$PR")
case "$COV" in
  "")        echo "  ➖ Codecov: yorum yok (codecov/patch check'i CI'da görünür)";;
  covered)   echo "  ✅ Codecov: tüm değişen satırlar test-kapsamında (patch tam)";;
  *)         echo "  📊 Codecov: $COV";;
esac

# ── Verdict ──
echo "──"
if [ "$STATE" != "OPEN" ]; then
  echo "  ↪︎ PR $STATE (gate n/a)"; exit 0
fi
if [ "$CI_OK" = 1 ] && [ "$CX" = clean ]; then
  echo "  🟢 MERGE-OK: CI yeşil + Codex temiz"; exit 0
fi
REASON=""
[ "$CI_OK" = 1 ] || REASON="$REASON CI-değil;"
[ "$CX" = clean ] || REASON="$REASON Codex=$CX;"
echo "  🔴 HENÜZ DEĞİL:$REASON"
exit 1
