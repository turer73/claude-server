#!/usr/bin/env bash
# koken-akademi self-hosted GitHub Actions runner — GÜVENLİ kurulum (Docker-ephemeral, owner-only).
#
# Eski host-tabanlı tasarımın (job'lar NOPASSWD-root klipperos olarak host'ta koşuyordu,
# Codex #222: 3×P1) yerini alır (#1175 güvenlik-rework). Yeni güvenlik modeli:
#   - Job'lar TAZE container'da non-root 'runner' olarak koşar → host-sudo/mount YOK,
#     job-arası state-sızıntısı YOK (--rm + --ephemeral).
#   - Dedicated 'kokenrunner' host-kullanıcısı: login YOK, SUDO YOK; yalnız docker-run +
#     token-mint yapan orkestratör. İş yükü container'da izole.
#   - Fine-grained PAT host'ta root-only (0640); container'a yalnız kısa-ömürlü
#     registration-token girer (PAT job'a ASLA ulaşmaz).
#
# Tehdit modeli: OWNER-ONLY (yalnız sahip-tetikli build/deploy). Fork/dış-PR kodu BU runner'da
# çalıştırılmamalı (workflow'da pull_request_target + self-hosted label kombinasyonu yasak).
set -euo pipefail

REPO="${KOKEN_RUNNER_REPO:-turer73/koken-akademi}"
RUNNER_USER="kokenrunner"
IMAGE="koken-gh-runner:latest"
SERVICE_NAME="koken-runner"
PAT_DIR="/etc/koken-runner"
# Build-context (Dockerfile/entrypoint/wrapper/service) kaynağı. Öncelik:
#   1) KOKEN_RUNNER_SRC (explicit override)
#   2) ../extensions/gh-runner (git-checkout'tan koşturma — operatör-sahipli, güvenilir)
#   3) /usr/local/lib/koken-runner/gh-runner (install.sh deployment'ta ROOT-sahipli kopya;
#      app-user'a chown'lu /opt'tan DEĞİL — aiserver tamper edemez, Codex install-hardening)
if [ -n "${KOKEN_RUNNER_SRC:-}" ]; then
    HERE="$KOKEN_RUNNER_SRC"
elif HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../extensions/gh-runner" 2>/dev/null && pwd)" \
        && [ "$(stat -c %U "$HERE" 2>/dev/null)" != "aiserver" ]; then
    # Adjacent kaynak app-user'a (aiserver) ait DEĞİLSE kabul: operatör git-checkout'u root/
    # klipperos-sahipli → güvenilir. install.sh-deployment'ta /opt aiserver'a chown'lu → reddet,
    # root-sahipli /usr/local snapshot'ına düş (Codex P1 :29). DISCRIMINATOR=OWNERSHIP, path-prefix
    # DEĞİL: bu repo'nun checkout'u /opt/linux-ai-server'da yaşıyor, path-guard onu da yanlışlıkla
    # reddedip dokümante 'sudo bash scripts/setup-gh-runner.sh'ı kırardı (regresyon-fix).
    :
else
    HERE="/usr/local/lib/koken-runner/gh-runner"
fi

echo "=== koken-akademi GÜVENLİ runner kurulumu (Docker-ephemeral, owner-only) ==="

# 0) ESKİ güvensiz runner'ı (#222, host-tabanlı NOPASSWD-root) emekliye ayır.
#    Aksi halde eski 'actions-runner-koken' unit'i ayakta kalır ve self-hosted,linux,klipper
#    label'ıyla owner-tetikli job'ları HOST'ta kapmaya devam eder → bu PR'ın amacı boşa çıkar
#    (Codex P1 :20). Bu host'ta kurulmadıysa no-op (idempotent).
LEGACY_SERVICE="actions-runner-koken"
LEGACY_DIR="/opt/actions-runner/koken"
if systemctl list-unit-files 2>/dev/null | grep -q "^${LEGACY_SERVICE}\.service"; then
    echo "  ESKİ güvensiz runner bulundu (${LEGACY_SERVICE}) → durdur + disable + kaldır"
    # FAIL-CLOSED (Codex P1 :45): disable başarısız olursa (timeout/systemd-red) unit'i SİLME +
    # devam etme. Aksi halde eski root-capable runner KOŞMAYA devam edip job kapar; unit-dosyası
    # silinince de fark edilmez. Durdur → gerçekten inactive mi DOĞRULA → ancak sonra kaldır.
    if ! sudo systemctl disable --now "${LEGACY_SERVICE}"; then
        echo "HATA: ${LEGACY_SERVICE} durdurulamadı — fail-closed, kurulum durduruldu." >&2
        echo "      Manuel: sudo systemctl stop ${LEGACY_SERVICE} && sudo systemctl disable ${LEGACY_SERVICE}" >&2
        exit 1
    fi
    if systemctl is-active --quiet "${LEGACY_SERVICE}"; then
        echo "HATA: ${LEGACY_SERVICE} disable sonrası HÂLÂ aktif — fail-closed, kurulum durduruldu." >&2
        exit 1
    fi
    # Eski unit KillMode=process kullanıyordu → 'disable --now' yalnız ANA süreci öldürür; job
    # child'ları (klipperos-sudo yüzeyli Runner.Worker) sağ kalabilir, is-active de geçer (Codex
    # P1 :55). cgroup'u zorla öldür + artık Runner süreçlerini temizle.
    sudo systemctl kill --kill-whom=all --signal=SIGKILL "${LEGACY_SERVICE}" 2>/dev/null || true
    sudo pkill -9 -f "${LEGACY_DIR}/.*Runner\.(Listener|Worker)" 2>/dev/null || true
    # GitHub kaydını da sök (config.sh remove); token gerekiyorsa kullanıcı manuel tamamlar.
    if [ -x "${LEGACY_DIR}/config.sh" ]; then
        echo "  NOT: eski runner GitHub kaydı kalmış olabilir — gerekirse manuel kaldır:"
        echo "       (cd ${LEGACY_DIR} && sudo -u <eski-kullanıcı> ./config.sh remove --token <REMOVE_TOKEN>)"
    fi
    sudo rm -f "/etc/systemd/system/${LEGACY_SERVICE}.service"
    sudo systemctl daemon-reload
    echo "  eski runner emekliye ayrıldı (durduruldu+doğrulandı; host'ta artık job kapmıyor)."
fi

# 1) Önkoşullar
command -v docker >/dev/null || { echo "HATA: docker bulunamadı"; exit 1; }
command -v jq >/dev/null || { echo "HATA: jq bulunamadı"; exit 1; }
[ -f "$HERE/Dockerfile" ] || { echo "HATA: $HERE/Dockerfile yok"; exit 1; }

# 2) Dedicated least-privilege kullanıcı: login YOK, SUDO YOK, docker grubunda.
#    (docker-run için docker grubu gerekir; iş yükü container'da izole olduğundan owner-only
#     modelde kabul edilebilir — eski tasarımdaki host-NOPASSWD-root'tan kat kat güvenli.)
if ! id "$RUNNER_USER" >/dev/null 2>&1; then
    sudo useradd --system --create-home --shell /usr/sbin/nologin "$RUNNER_USER"
    echo "  kullanıcı oluşturuldu: $RUNNER_USER (nologin, sudo YOK)"
fi
sudo usermod -aG docker "$RUNNER_USER"

# 3) Runner image build (sürüm pinli — reproducible).
#    Build context'i ROOT-sahipli snapshot'tan al: kurulu deployment'ta install.sh /opt'u
#    app-kullanıcısına (aiserver) chown'lar → $HERE/{Dockerfile,entrypoint.sh} aiserver-
#    yazılabilir olur. $HERE'den direkt build edilirse aiserver, image'a kod enjekte edip
#    (sonraki owner-rebuild'de pişer) container-içi REG_TOKEN'ı sızdırabilir / deploy'u
#    kurcalayabilir. Root-sahipli kopyadan build → aiserver build-input'unu tamper edemez.
BUILD_DIR="/usr/local/lib/koken-runner/build"
sudo rm -rf "$BUILD_DIR"
sudo install -d -m 0755 -o root -g root "$BUILD_DIR"
sudo cp -r "$HERE/." "$BUILD_DIR/"
sudo chown -R root:root "$BUILD_DIR"
RUNNER_VERSION=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r '.tag_name' | sed 's/^v//')
[ -n "$RUNNER_VERSION" ] || { echo "HATA: runner sürümü alınamadı"; exit 1; }
echo "  runner sürümü: $RUNNER_VERSION → image build ($IMAGE, root-sahipli context)"
sudo docker build --build-arg RUNNER_VERSION="$RUNNER_VERSION" -t "$IMAGE" "$BUILD_DIR"

# 4) PAT dizini (dosyayı KULLANICI koyacak; script PAT istemez/saklamaz)
sudo install -d -m 0750 -o root -g "$RUNNER_USER" "$PAT_DIR"

# 5) Log dosyası (orkestratör yazabilir) + rotation.
sudo install -m 0640 -o "$RUNNER_USER" -g "$RUNNER_USER" /dev/null /var/log/koken-runner.log
# logrotate (Codex P2 :53): uzun-ömürlü servis tüm container stdout/stderr'ini bu dosyaya append
# eder; repo logrotate'i bu yolu KAPSAMIYOR → sınırsız büyür, disk doldurur. copytruncate:
# wrapper dosyayı `>>` ile açık tutar, in-place truncate ile process-restart gerekmez.
sudo install -m 0644 /dev/stdin /etc/logrotate.d/koken-runner <<'LOGROTATE'
/var/log/koken-runner.log {
    weekly
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
LOGROTATE

# 6) Orkestratör wrapper'ı ROOT-sahipli sabit yola kur (app-user değiştiremez, Codex :10).
#    Servis bu kopyadan koşar; /opt çalışma-ağacındaki kopya değil. Kaynak = root-sahipli
#    BUILD_DIR snapshot'ı (image ile aynı güven sınırı).
sudo install -D -m 0755 -o root -g root \
    "$BUILD_DIR/run-ephemeral-loop.sh" /usr/local/lib/koken-runner/run-ephemeral-loop.sh

# 7) systemd unit (root-sahipli snapshot'tan)
sudo install -m 0644 "$BUILD_DIR/koken-runner.service" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo systemctl daemon-reload

cat <<EOF

=== KURULUM TAMAM (image + kullanıcı + servis hazır) ===
SON ADIMLAR (manuel — güvenlik gereği PAT'ı script saklamaz):
  1) GitHub fine-grained PAT oluştur:
       - Repository access: yalnız ${REPO}
       - Permissions: Administration -> Read and write (registration-token mint için)
  2) PAT'ı root-only yerleştir — token'ı KOMUT SATIRINA/SHELL GEÇMİŞİNE yazma (read ile gizli
     gir; değer argv'de değil stdin'den akar):
       sudo install -m 0640 -o root -g ${RUNNER_USER} /dev/null ${PAT_DIR}/pat
       read -rs KOKEN_PAT && printf '%s' "\$KOKEN_PAT" | sudo tee ${PAT_DIR}/pat >/dev/null && unset KOKEN_PAT
  3) Servisi başlat:
       sudo systemctl enable --now ${SERVICE_NAME}
  4) İzle:
       journalctl -u ${SERVICE_NAME} -f   ve   tail -f /var/log/koken-runner.log

GÜNCELLEME (runner 30-gün-deadline'ı, Codex :109): ayda bir setup'ı yeniden koş → image'ı taze
build eder (en yeni runner sürümü) -> 'sudo systemctl restart ${SERVICE_NAME}'.
  - install.sh deployment'ta: 'sudo koken-runner-setup' (ROOT-sahipli kopya; /opt'taki app-user-
    yazılabilir script DEĞİL — aiserver tamper edip root-exec'e çeviremesin diye, Codex).
  - git-checkout'tan: 'sudo bash scripts/setup-gh-runner.sh' (operatör-sahipli ağaç).
EOF
