#!/usr/bin/env bash
# queue-merged-for-review.sh — origin/master'a yeni giren commit'lerin değişen kod-dosyalarını
# code-review kuyruğuna yaz → Haiku-review ajanı (_drain_queue) inceler.
#
# NEDEN: merge-akışımız (worktree-commit → push → GitHub squash-merge → `git reset --mixed`)
# LOKAL post-commit hook'u TETİKLEMEZ → merge edilen kod Haiku-review'a hiç girmez. Bu script
# origin/master ilerlemesini izleyip aradaki diff'i kuyruklayarak o boşluğu kapatır (cron'dan).
# Salt-okunur dışında tek yan-etki: kuyruk dosyasına satır ekler. OUTCOME marker cron-wrap için.
set -uo pipefail
cd /opt/linux-ai-server || { echo "OUTCOME: fail | cd"; exit 0; }

QUEUE="data/code-review-queue.txt"
STATE="data/hook-state/last-reviewed-sha"
mkdir -p data/hook-state

git fetch origin master -q 2>/dev/null || { echo "OUTCOME: partial | fetch-fail"; exit 0; }
NEW=$(git rev-parse origin/master 2>/dev/null) || { echo "OUTCOME: fail | rev-parse"; exit 0; }

# İlk çalışma: baseline = mevcut HEAD (geçmişi kuyruklamaz, yalnız BUNDAN SONRAKİ merge'ler).
if [ ! -f "$STATE" ]; then
    echo "$NEW" > "$STATE"
    echo "OUTCOME: pass | baseline set ($NEW), 0 dosya"
    exit 0
fi
LAST=$(cat "$STATE" 2>/dev/null)
if [ "$LAST" = "$NEW" ]; then
    echo "OUTCOME: pass | yeni-commit yok (0 dosya)"
    exit 0
fi

# LAST..NEW arası değişen kod-dosyaları (post-commit hook ile aynı uzantı seti). LAST geçersizse
# (force-push/rebase) tek-commit diff'e düş.
RANGE="${LAST}..${NEW}"
git rev-parse --verify -q "$LAST^{commit}" >/dev/null 2>&1 || RANGE="${NEW}~1..${NEW}"

FILES=$(git diff --name-only "$RANGE" 2>/dev/null | grep -E '\.(py|sh|ts|tsx|js|sql)$' || true)
N=0
if [ -n "$FILES" ]; then
    # yalnız HÂLÂ var olan dosyalar (silinenler review-edilmez)
    while IFS= read -r f; do
        [ -n "$f" ] && [ -f "$f" ] && { echo "$f" >> "$QUEUE"; N=$((N + 1)); }
    done <<< "$FILES"
fi

echo "$NEW" > "$STATE"
echo "OUTCOME: pass | ${RANGE} → ${N} dosya kuyruğa (Haiku-review)"
exit 0
