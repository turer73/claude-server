#!/usr/bin/env bash
# GitHub Actions self-hosted runner kurulum scripti
# koken-akademi repo'su için ephemeral runner (Klipper SER8)
#
# Kullanım: bash /opt/linux-ai-server/scripts/setup-gh-runner.sh
# Gereksinim: sudo yetkisi (klipperos NOPASSWD:ALL → OK)

set -euo pipefail

RUNNER_DIR="/opt/actions-runner/koken"
RUNNER_USER="klipperos"
RUNNER_NAME="klipper"
RUNNER_LABELS="self-hosted,linux,klipper"
REPO_URL="https://github.com/turer73/koken-akademi"
SERVICE_NAME="actions-runner-koken"
WRAPPER_SCRIPT="$RUNNER_DIR/start-ephemeral-runner.sh"

echo "=== GH-RUNNER-20260623-01: koken-akademi self-hosted runner kurulumu ==="
echo ""

# Ön kontrol: gh auth
echo "[1/8] gh auth kontrol..."
if ! env -u GITHUB_TOKEN gh auth status 2>&1 | grep -q "Logged in"; then
    echo "HATA: gh auth başarısız. 'gh auth login' ile oturum aç."
    exit 1
fi
echo "  OK: gh turer73 hesabı aktif"

# Ön kontrol: bağımlılıklar
echo "[2/8] Bağımlılık kontrol..."
node_ver=$(node --version 2>/dev/null) && echo "  node: $node_ver" || { echo "HATA: node bulunamadı"; exit 1; }
git_ver=$(git --version 2>/dev/null) && echo "  git: $git_ver" || { echo "HATA: git bulunamadı"; exit 1; }

# Runner dizini oluştur
echo "[3/8] Dizin oluşturuluyor: $RUNNER_DIR..."
sudo mkdir -p "$RUNNER_DIR"
sudo chown "$RUNNER_USER:$RUNNER_USER" "$RUNNER_DIR"
echo "  OK"

# En son runner versiyonunu al
echo "[4/8] GitHub Actions runner son versiyon alınıyor..."
RUNNER_VERSION=$(env -u GITHUB_TOKEN gh api repos/actions/runner/releases/latest --jq '.tag_name' | sed 's/v//')
echo "  Versiyon: $RUNNER_VERSION"

RUNNER_TARBALL="actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
RUNNER_URL="https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/${RUNNER_TARBALL}"

# Runner indir
echo "[5/8] Runner indiriliyor..."
if [ ! -f "$RUNNER_DIR/$RUNNER_TARBALL" ]; then
    curl -fsSL -o "$RUNNER_DIR/$RUNNER_TARBALL" "$RUNNER_URL"
    echo "  İndirildi: $RUNNER_TARBALL"
else
    echo "  Zaten mevcut: $RUNNER_TARBALL"
fi

# Çıkar
echo "  Çıkarılıyor..."
cd "$RUNNER_DIR"
tar xzf "$RUNNER_TARBALL" --overwrite
echo "  OK"

# İlk yapılandırma (registration token alıp config.sh çalıştır)
echo "[6/8] Runner yapılandırılıyor (ephemeral)..."
REG_TOKEN=$(env -u GITHUB_TOKEN gh api \
    "repos/turer73/koken-akademi/actions/runners/registration-token" \
    --method POST --jq '.token')

# Eski config varsa temizle
rm -f "$RUNNER_DIR/.runner" "$RUNNER_DIR/.credentials" "$RUNNER_DIR/.credentials_rsaparams" 2>/dev/null || true

./config.sh \
    --url "$REPO_URL" \
    --token "$REG_TOKEN" \
    --name "$RUNNER_NAME" \
    --labels "$RUNNER_LABELS" \
    --ephemeral \
    --unattended \
    --disableupdate
echo "  OK: runner yapılandırıldı"

# Wrapper script oluştur (systemd her restart'ta ephemeral re-register)
echo "[7/8] Ephemeral wrapper script oluşturuluyor..."
cat > "$WRAPPER_SCRIPT" << 'WRAPPER_EOF'
#!/usr/bin/env bash
# Ephemeral runner wrapper — systemd her restart'ta çalıştırır
# Her çalışmada yeni token alır ve runner'ı re-register eder
set -euo pipefail

RUNNER_DIR="/opt/actions-runner/koken"
cd "$RUNNER_DIR"

# Önceki ephemeral state'i temizle
rm -f .runner .credentials .credentials_rsaparams 2>/dev/null || true

# GitHub OAuth token'ı al (GITHUB_TOKEN env'si yoksa keyring'den)
REG_TOKEN=$(env -u GITHUB_TOKEN gh api \
    "repos/turer73/koken-akademi/actions/runners/registration-token" \
    --method POST --jq '.token')

# Ephemeral yapılandır
./config.sh \
    --url https://github.com/turer73/koken-akademi \
    --token "$REG_TOKEN" \
    --name klipper \
    --labels self-hosted,linux,klipper \
    --ephemeral \
    --unattended \
    --disableupdate

# Runner'ı çalıştır (job bittikten sonra exit 0 ile çıkar)
exec ./run.sh
WRAPPER_EOF

chmod +x "$WRAPPER_SCRIPT"
echo "  OK: $WRAPPER_SCRIPT"

# Systemd service kur
echo "[8/8] Systemd servisi kuruluyor: $SERVICE_NAME..."
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

sudo tee "$SERVICE_FILE" > /dev/null << SERVICE_EOF
[Unit]
Description=GitHub Actions Runner - koken-akademi (ephemeral, klipper)
After=network-online.target
Wants=network-online.target
# Ephemeral runner her job sonrası exit-0 yapar; Restart=always re-register için DOĞRU.
# Ama config/run KALICI fail ederse (bozuk token, ağ, repo erişimi) runner anında çıkar
# ve RestartSec=5 ile her 5sn'de yeni registration-token ister -> GitHub API spam/rate-limit
# busyloop'u. StartLimit: 5dk içinde >5 restart olursa servisi durdur (failed state) ->
# normal işleyişte (job-başına ~1 restart) asla tetiklenmez, kalıcı-fail'de loop'u keser.
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=$RUNNER_USER
WorkingDirectory=$RUNNER_DIR
ExecStart=$WRAPPER_SCRIPT
Restart=always
# Çıkış→re-register arası bekleme; spike'ı yumuşatır (job-başına tek restart'ta görünmez).
RestartSec=10
KillMode=process
KillSignal=SIGTERM
TimeoutStopSec=5min
Environment=HOME=/home/$RUNNER_USER

[Install]
WantedBy=multi-user.target
SERVICE_EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl start "$SERVICE_NAME"
echo "  OK: servis aktif"

echo ""
echo "=== KURULUM TAMAMLANDI ==="
echo ""
echo "Durum:  sudo systemctl status $SERVICE_NAME"
echo "Loglar: journalctl -u $SERVICE_NAME -f"
echo "GitHub: https://github.com/turer73/koken-akademi/settings/actions/runners"
echo ""
echo "Klipper runner GitHub'da 'Idle' görünmeli."
echo "surer'a runner-aktif bildir → deploy.yml'de runs-on: [self-hosted, klipper] yap."
