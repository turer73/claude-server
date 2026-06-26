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
CONTAINER_NAME="${KOKEN_RUNNER_CONTAINER:-koken-job}"      # sabit container adı → stop'ta temizlenebilir

log() { echo "[$(date -u +%FT%TZ)] $*" >>"$LOG" 2>/dev/null || echo "[log] $*"; }

# systemctl stop/restart'ta: docker run --rm CLIENT'ı systemd öldürse de container ayrı süreç
# (systemd-child değil) → SIGTERM'i yutan bir job ile sağ kalır, shutdown-izolasyonu bozulurdu
# (Codex :49). Trap ile sinyalde container'ı zorla kaldır; çıkışta da temizle.
cleanup() { docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true; }
trap 'cleanup; exit 143' TERM INT

mint_token() {
    # registration-token mint (PAT host'ta kalır, dönen token kısa-ömürlü). Boş→fail.
    local pat
    pat=$(cat "$PAT_FILE" 2>/dev/null) || return 1
    [ -n "$pat" ] || return 1
    # Authorization header argv'de GÖRÜNMEZ: --config - ile stdin'den okunur. Aksi halde PAT
    # `curl -H "Bearer ..."` argümanına girer, /proc/<pid>/cmdline ile başka unprivileged
    # kullanıcıya (örn. aiserver) sızar — 0640 dosya korumasını boşa çıkarır (Codex :27).
    printf 'header = "Authorization: Bearer %s"\n' "$pat" \
        | curl -fsS --max-time 20 -X POST --config - \
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
    # REG_TOKEN argv'de GÖRÜNMEZ: değer orkestratörün env'inden geçer (-e REG_TOKEN, =value YOK).
    # /proc/<pid>/environ yalnız kokenrunner+root okuyabilir; /proc/<pid>/cmdline world-readable
    # olduğundan -e REG_TOKEN="$token" başka kullanıcıya kayıt-token'ı sızdırırdı (Codex :43).
    export REG_TOKEN="$token"
    # Önceki cycle'dan kalmış bayat container varsa (hard-crash) temizle: sabit --name çakışmasın.
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    # İzolasyon: --rm (taze), host-mount/socket YOK, non-root, --pull never (yerel image).
    # --name: stop/trap container'ı bulup kaldırabilsin (Codex :49).
    docker run --rm --pull never --name "$CONTAINER_NAME" \
        -e REG_TOKEN \
        -e REPO_URL="https://github.com/${REPO}" \
        -e RUNNER_NAME="$RUNNER_NAME" \
        "$IMAGE" >>"$LOG" 2>&1
    rc=$?
    unset REG_TOKEN
    # Ephemeral happy-path: config+run başarılı, TEK job sonrası rc=0 (job'un kendisi fail etse
    # bile run.sh 0 döner — sonuç GitHub'a raporlanır). rc!=0 → container BAŞLATILAMADI (125/126/
    # 127) VEYA config/run BAŞARISIZ (GitHub'a ulaşamadı / REPO_URL red / crash). Her iki halde
    # de backoff: aksi halde fails sıfırlanır, hiç runner koşmadan sonsuz token-mint=API-spam
    # olur (Codex :44/:57). Normal-tamamlanma yalnız rc=0 ile ayırt edilebilir.
    if [ "$rc" -ne 0 ]; then
        log "container/runner BAŞARISIZ (rc=$rc: başlatma/config/run) → backoff (no-runner API-spam'i önle)"
        return 1
    fi
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
