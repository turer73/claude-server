#!/bin/bash
# ci-fix-runall.sh — Otonom CI-fix hattini yeniden baglar (soft-gate shadow verisi uretir).
#
# NEDEN (2026-07-13, Turgut karari): /api/v1/ci/run-all 2026-05-01'de sessizce emekliye
# ayrilmisti — crontab repoya alinirken (05-03, 1c8a977) duz-pytest test-runner.sh ile
# degistirildi, o da attempt_fix'i HIC cagirmaz. action_review soft-gate (#1230) bu hatti
# shadow-gozlemlemek uzere 2026-07-03'te kuruldu ama gate'lenecek CANLI hat yoktu -> shadow
# SIFIR uretti (0 action-review/ci_fixer event, 07-03..07-10). Karar: tarihsel /ci/run-all
# cron'unu geri koy, GERCEK shadow koştur, sonra FLIP (soft-gate ON) degerlendir.
#
# NE YAPAR: PROJECT_REGISTRY'deki 9 projede test kosar, ILK fail'de attempt_fix (Claude Code,
# notify-only/gate-OFF) cagirir. Supheli-diff -> emit_event(action-review/ci_fixer, warn).
# ISTE SHADOW VERISI BUDUR — soft-gate precision'i bu event'lerden hesaplanacak.
#
# UYARI (Turgut-onayli blast-radius): attempt_fix hedef repo working-tree'sini DEGISTIRIR
# (commit/push YOK; ci-fixer-settings.json git commit/push'a izin vermez). Hedef 9 repo
# arasinda /opt/linux-ai-server (klipper) + /data/projects/* (surer aktif-branch DAHIL) var.
# Fix'ler shadow'da AUTO-ACCEPT edilir (yalniz supheliler loglanir) -> working-tree kirlenebilir.

set +e

API="${API:-http://localhost:8420}"

# INTERNAL_API_KEY: wrapper (klipper-cron-wrap.sh) .env'i zaten source eder; standalone kosumda
# fallback olarak biz de yukleriz. read: .env 1. satirdaki degeri al (dual-key notu: head -1).
if [ -z "${INTERNAL_API_KEY:-}" ] && [ -f /opt/linux-ai-server/.env ]; then
  set -a
  # shellcheck disable=SC1091
  . /opt/linux-ai-server/.env
  set +a
fi

if [ -z "${INTERNAL_API_KEY:-}" ]; then
  echo "OUTCOME: fail | INTERNAL_API_KEY bulunamadi (.env)" >&2
  exit 2
fi

# Transient bag­lanti-hatasi rc'leri: 7=connect-fail, 52=empty-reply, 56=recv-error.
# Bunlar "HIC yanit gelmedi" demek -> sunucu-tarafi is BASLAMADI (kismi-yan-etki yok) ->
# retry GUVENLI (2026-07-14: worker-recycle sirasinda 05:00-run rc=52 aldi, flood-koku).
# rc=28 (timeout) BILINCLI haric: /ci/run-all idempotent DEGIL (working-tree degistirir,
# Claude-fix kosar) -> timeout is uzun-kostu demek, retry cift-kosum riski. Sadece hizli-
# baglanti-hatalarini yeniden dene.
_TRANSIENT_RC=" 7 52 56 "
MAX_TRIES=3
RETRY_BACKOFF=20  # worker-recycle'in oturmasi icin

echo "ci-fix-runall: POST $API/api/v1/ci/run-all ($(date -Is))"

RESP=""
RC=0
for try in $(seq 1 "$MAX_TRIES"); do
  # /ci/run-all senkron: 9 proje test + fail'de Claude-fix. Fix varsa uzun surer -> genis
  # timeout (45dk). Bos-yesil suite'te hizli doner.
  RESP=$(curl -s --max-time 2700 -X POST "$API/api/v1/ci/run-all" \
    -H 'Content-Type: application/json' \
    -H "X-API-Key: ${INTERNAL_API_KEY}")
  RC=$?
  echo "ci-fix-runall deneme $try/$MAX_TRIES: rc=$RC resp=${RESP:0:2000}"

  # Basari (rc=0) VEYA transient-OLMAYAN hata -> dongu bitir (retry etme).
  if [ "$RC" -eq 0 ] || [ "${_TRANSIENT_RC/ $RC /}" = "$_TRANSIENT_RC" ]; then
    break
  fi
  # Transient + deneme kaldi -> backoff + retry.
  if [ "$try" -lt "$MAX_TRIES" ]; then
    echo "ci-fix-runall: transient rc=$RC (yanit yok, is baslamadi) -> ${RETRY_BACKOFF}s sonra retry"
    sleep "$RETRY_BACKOFF"
  fi
done

# OUTCOME-contract (tools/lint-cron-outcome.sh): wrapper bunu cron_outcomes'a yazar.
# rc!=0: curl transport-hatasi (timeout/connection) = fail. rc=0: HTTP basarili -> pass;
# icerikteki total_failed>0 NORMAL (fix denendi/shadow), transport-fail DEGIL. Shadow
# analizi event'lerde (action-review/ci_fixer), OUTCOME sadece calisti-mi sinyali.
if [ "$RC" -ne 0 ]; then
  echo "OUTCOME: fail | /ci/run-all cagrisi basarisiz (curl rc=$RC, timeout/connection)"
else
  echo "OUTCOME: pass | /ci/run-all tamam (shadow: action-review/ci_fixer event'lerine bak)"
fi

exit "$RC"
