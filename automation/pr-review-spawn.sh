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
# Max-plan ABONELİK kimliğini zorla (autonomous-claude.sh deseni): ANTHROPIC_API_KEY set'liyken
# claude CLI pay-as-you-go API kullanır → kredi bitince "Credit balance is too low" ile spawn DÜŞER.
# Strip → ~/.claude OAuth (Max-plan) = sıfır API faturası. Script claude dışında key kullanmıyor.
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
LOG="${PR_REVIEW_SPAWN_LOG:-/opt/linux-ai-server/data/hook-logs/pr-review-spawn.log}"
SPAWN_ENABLED="${SPAWN_ENABLED:-0}"
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
log() { echo "[$(date -Iseconds)] $1" | tee -a "$LOG"; }

[ -d "$LOCAL" ] || { log "FAIL: local checkout yok: $LOCAL"; echo "OUTCOME: fail | $REPO#$PR checkout-yok"; exit 3; }

cd "$LOCAL" || { log "FAIL cd $LOCAL"; echo "OUTCOME: fail | cd-fail"; exit 3; }

# ── FAZ4-S3: blast-radius (değişiklik-öncesi-etki) enjeksiyonu ──────────────
# SADECE claude-server PR'ları (blast-radius.sh klipper-spesifik: app.core/db
# tablo-consumer analizi). read-only: gh + git fetch + diff --name-only + statik
# analiz. FAIL-SAFE: herhangi bir hata -> blok BOŞ, review yine çalışır (enrichment,
# gate DEĞİL). Not: blast-radius working-tree(master) içeriğini analiz eder ->
# "master-içerik yaklaşımı" (PR-HEAD değil), prompt'ta açıkça belirtilir.
BLAST_BLOCK=""
BR_SH="/opt/linux-ai-server/scripts/blast-radius.sh"
if [ "$NAME" = "claude-server" ] && [ -x "$BR_SH" ]; then
    # timeout: ag-op asilirsa (gh API / fetch) fail-safe devreye girsin —
    # aksi halde enrichment spawn-oncesi SURESIZ bloklar (cron-wrap'ta outer
    # timeout yok). timeout/124 -> bos deger -> blok atlanir (Codex P2).
    BASE_SHA="$(timeout 15 gh pr view "$PR" -R "$REPO" --json baseRefOid -q .baseRefOid 2>/dev/null || true)"
    if timeout 30 git fetch -q origin "pull/$PR/head" 2>/dev/null; then
        BR_RANGE="${BASE_SHA:-origin/master}...FETCH_HEAD"
        BR_OUT="$(timeout 30 "$BR_SH" --diff "$BR_RANGE" 2>/dev/null || true)"
        if [ -n "$BR_OUT" ]; then
            BLAST_BLOCK="
EK BAĞLAM — BLAST-RADIUS (otomatik, FAZ4-S3; master-içerik yaklaşımı, PR-HEAD değil):
$BR_OUT
Yukarıdaki consumer'lar diff-DIŞI dosyalardır ama bu PR'ın DB-tablo/şema/sorgu
değişiklikleri onları kırabilir. Review'da özellikle şema/kolon/sorgu değişimlerinde
bu etki-alanını göz önünde tut (bu blok diff yerine GEÇMEZ, tamamlayıcıdır)."
            log "blast-radius enjekte edildi: $REPO#$PR ($BR_RANGE, ${#BR_OUT} bayt)"
        else
            log "blast-radius bos cikti (taranabilir degisen dosya yok?): $REPO#$PR"
        fi
    else
        log "blast-radius atlandi (git fetch pull/$PR/head basarisiz): $REPO#$PR"
    fi
fi

# Bot-etiketli, TEK özet-comment talimatlı review prompt (pilot: direct review,
# multi-agent /code-review fan-out DEĞİL -> Max-x20 bütçe-dostu + basit).
read -r -d '' PROMPT <<PEOF || true
Sen otomatik bir PR-review ajanisin (pilot). Gorev (SADECE bunlar; baska hicbir sey yapma):
1. Bu PR'in diff'ini incele: gh pr diff $PR -R $REPO
2. SADECE DIFF uzerinden review et (correctness-bug: inverted-condition, off-by-one, null-deref, missing-await, falsy-zero, sessiz-hata-yutma, escape eksikligi, removed-guard). ONEMLI: working-tree PR-HEAD DEGIL (checkout yok) -> repo dosyalarini OKUMA/guvenme (master'i okursun, PR'in degil = yaniltici). Yalniz diff-icerigine dayan; diff-disi-baglam gerekirse "diff-disi-dogrulama gerek" diye not dus.${BLAST_BLOCK}
3. Bulgulari TEK bir ozet PR-comment olarak post et: gh pr comment $PR -R $REPO --body "..."
   Comment'in EN BASINA bu prefix'i koy (aynen):
   [otomatik review - klipper; FP olabilir, insan-dogrula]
   Sonra bulgulari kisa madde-listesi olarak yaz (dosya:satir + tek-cumle). Bulgu yoksa "Belirgin correctness-bulgu yok" yaz.
KISIT: Kod DEGISTIRME, commit/push/merge YAPMA, baska dosyaya dokunMA. Sadece oku + tek-comment. Bittiginde "OUTCOME: pass | reviewed $REPO#$PR" yaz.
PEOF

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
  exit 1  # poller'a FAIL bildir -> mark_reviewed/daily_inc YAPMASIN (basarisiz
          # review'i 'reviewed' sayma; sonraki run tekrar dener). Codex-P1.
fi
