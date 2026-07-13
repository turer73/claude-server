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
  echo "ci-fix-runall: INTERNAL_API_KEY bulunamadi (.env) -> abort" >&2
  exit 2
fi

echo "ci-fix-runall: POST $API/api/v1/ci/run-all ($(date -Is))"

# /ci/run-all senkron: 9 proje test + fail'de Claude-fix. Fix varsa uzun surer -> genis timeout
# (45dk). Bos-yesil suite'te hizli doner. Timeout = transport-fail (outcome fail).
RESP=$(curl -s --max-time 2700 -X POST "$API/api/v1/ci/run-all" \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: ${INTERNAL_API_KEY}")
RC=$?

echo "ci-fix-runall rc=$RC resp=${RESP:0:2000}"

# rc!=0: curl transport-hatasi (timeout/connection) -> wrapper outcome=fail. Icerikteki
# total_failed>0 NORMAL (fix denendi) -> HTTP basariliysa exit 0; shadow-analizi event'lerde.
exit "$RC"
