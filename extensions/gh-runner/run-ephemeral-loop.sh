#!/usr/bin/env bash
# Host orkestratör (systemd, 'kokenrunner' kullanıcısı — SUDO YOK, yalnız docker grubu).
# Job-cycle'ı KENDİ içinde döngüler: her job için taze ephemeral container başlatır.
# Bu yüzden normal job'lar systemd-restart DEĞİL → StartLimitBurst gerekmez (#1175 fix:
# eski guard normal job-yükünü kırıyordu; eski Restart=always ise fail'de busyloop yapıyordu).
#
# Güvenlik: PAT host'ta root-only kalır; mint burada (host) yapılır, container'a yalnız
# kısa-ömürlü registration-token (-e REG_TOKEN) geçer. Container'ın host-mount/socket'i yok.
set -uo pipefail

PAT_FILE="${KOKEN_RUNNER_PAT_FILE:-/etc/koken-runner/pat}"   # root:kokenrunner 0640
REPO="${KOKEN_RUNNER_REPO:-turer73/koken-akademi}"
IMAGE="${KOKEN_RUNNER_IMAGE:-koken-gh-runner:latest}"
LOG="${KOKEN_RUNNER_LOG:-/var/log/koken-runner.log}"
BACKOFF_BASE="${KOKEN_RUNNER_BACKOFF_BASE:-30}"             # mint-fail başına +Ns (cap 300)
MAX_CYCLES="${KOKEN_RUNNER_MAX_CYCLES:-0}"                  # 0=sonsuz (test için sınırla)
RUNNER_NAME="${KOKEN_RUNNER_NAME:-koken-$(hostname -s)}"    # sabit ad → --replace bayatı devralır

log() { echo "[$(date -u +%FT%TZ)] $*" >>"$LOG" 2>/dev/null || echo "[log] $*"; }

mint_token() {
    # registration-token mint (PAT host'ta kalır, dönen token kısa-ömürlü). Boş→fail.
    local pat
    pat=$(cat "$PAT_FILE" 2>/dev/null) || return 1
    [ -n "$pat" ] || return 1
    curl -fsS --max-time 20 -X POST \
        -H "Authorization: Bearer ${pat}" \
        -H "Accept: application/vnd.github+json" \
        -H "X-GitHub-Api-Version: 2022-11-28" \
        "https://api.github.com/repos/${REPO}/actions/runners/registration-token" \
        | jq -r '.token // empty'
}

run_one_cycle() {
    local token rc
    token=$(mint_token) || token=""
    if [ -z "$token" ]; then
        return 1
    fi
    log "yeni job-cycle: taze ephemeral container (state izolasyonu)"
    # İzolasyon: --rm (taze), host-mount/socket YOK, non-root, --pull never (yerel image).
    docker run --rm --pull never \
        -e REG_TOKEN="$token" \
        -e REPO_URL="https://github.com/${REPO}" \
        -e RUNNER_NAME="$RUNNER_NAME" \
        "$IMAGE" >>"$LOG" 2>&1
    rc=$?
    # docker container'ı BAŞLATAMADIysa (rc=125 daemon/socket/image-yok; 126/127 entrypoint
    # exec edilemedi) bu mint-fail gibi backoff ister: aksi halde return 0 → fails sıfırlanır →
    # hiç runner koşmadan sonsuz token-mint = API-spam (Codex :44). Container KOŞTUYSA (rc 0 veya
    # job-fail/idle) cycle başarılı: --ephemeral tek-job sonrası taze cycle normaldir.
    if [ "$rc" -eq 125 ] || [ "$rc" -eq 126 ] || [ "$rc" -eq 127 ]; then
        log "docker container BAŞLATILAMADI (rc=$rc: daemon/image/socket) → başlatma-hatası, backoff"
        return 1
    fi
    [ "$rc" -ne 0 ] && log "container rc=$rc (job-fail/idle, normal — sonraki cycle taze)"
    return 0
}

fails=0
cycles=0
while true; do
    if run_one_cycle; then
        fails=0
    else
        fails=$((fails + 1))
        backoff=$((fails * BACKOFF_BASE))
        [ "$backoff" -gt 300 ] && backoff=300
        log "registration-token mint başarısız (#${fails}) → ${backoff}s backoff (API-spam yok)"
        sleep "$backoff"
    fi
    cycles=$((cycles + 1))
    [ "$MAX_CYCLES" -gt 0 ] && [ "$cycles" -ge "$MAX_CYCLES" ] && break
done
