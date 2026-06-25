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
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../extensions/gh-runner" && pwd)"

echo "=== koken-akademi GÜVENLİ runner kurulumu (Docker-ephemeral, owner-only) ==="

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

# 3) Runner image build (sürüm pinli — reproducible)
RUNNER_VERSION=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r '.tag_name' | sed 's/^v//')
[ -n "$RUNNER_VERSION" ] || { echo "HATA: runner sürümü alınamadı"; exit 1; }
echo "  runner sürümü: $RUNNER_VERSION → image build ($IMAGE)"
sudo docker build --build-arg RUNNER_VERSION="$RUNNER_VERSION" -t "$IMAGE" "$HERE"

# 4) PAT dizini (dosyayı KULLANICI koyacak; script PAT istemez/saklamaz)
sudo install -d -m 0750 -o root -g "$RUNNER_USER" "$PAT_DIR"

# 5) Log dosyası (orkestratör yazabilir)
sudo install -m 0640 -o "$RUNNER_USER" -g "$RUNNER_USER" /dev/null /var/log/koken-runner.log

# 6) systemd unit
sudo install -m 0644 "$HERE/koken-runner.service" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo systemctl daemon-reload

cat <<EOF

=== KURULUM TAMAM (image + kullanıcı + servis hazır) ===
SON ADIMLAR (manuel — güvenlik gereği PAT'ı script saklamaz):
  1) GitHub fine-grained PAT oluştur:
       - Repository access: yalnız ${REPO}
       - Permissions: Administration -> Read and write (registration-token mint için)
  2) PAT'ı root-only yerleştir:
       printf '%s' '<PAT>' | sudo install -m 0640 -o root -g ${RUNNER_USER} /dev/stdin ${PAT_DIR}/pat
  3) Servisi başlat:
       sudo systemctl enable --now ${SERVICE_NAME}
  4) İzle:
       journalctl -u ${SERVICE_NAME} -f   ve   tail -f /var/log/koken-runner.log

GÜNCELLEME (runner 30-gün-deadline'ı, Codex :109): ayda bir 'bash scripts/setup-gh-runner.sh'
ile image'ı yeniden build et (en yeni runner sürümünü çeker) -> 'sudo systemctl restart ${SERVICE_NAME}'.
EOF
