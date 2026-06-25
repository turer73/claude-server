#!/usr/bin/env bash
# Container-içi entrypoint (non-root 'runner'): kısa-ömürlü REG_TOKEN ile kaydol, TEK job
# çalıştır, çık. --ephemeral → job sonrası otomatik dekaydol; container --rm ile silinir
# (job-arası state izolasyonu). PAT container'a GİRMEZ — yalnız registration-token alır.
set -euo pipefail

: "${REG_TOKEN:?REG_TOKEN gerekli - host tarafinda mint edilir}"
: "${REPO_URL:?REPO_URL gerekli}"
RUNNER_LABELS="${RUNNER_LABELS:-self-hosted,linux,klipper,docker}"

# --replace: aynı-adlı bayat/offline kayıt varsa devral (kayıt-çakışması fix, Codex #222 :76).
# --unattended: prompt yok. --ephemeral: tek-job + oto-dekaydol.
./config.sh \
    --url "$REPO_URL" \
    --token "$REG_TOKEN" \
    --name "koken-$(hostname)" \
    --labels "$RUNNER_LABELS" \
    --ephemeral \
    --replace \
    --unattended

# run.sh tek job'ı bekler+koşar, sonra çıkar (ephemeral). exec → PID 1 sinyalleri alır.
exec ./run.sh
