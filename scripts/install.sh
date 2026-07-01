#!/bin/bash
set -e

echo "=== Linux-AI Server Installer ==="

# Installer checkout kökü — operatör-sahipli GÜVENİLİR kaynak. TÜM kopyalar buradan okunur:
# /opt'a kopyalanıp aiserver'a chown'landıktan SONRA oradan okumak, ele geçen aiserver'ın
# runner Dockerfile/wrapper/setup'ını root snapshot'lanmadan önce değiştirmesine izin verirdi
# (Codex P1 TOCTOU). Checkout'tan okumak bu yarışı kapatır.
if [ -n "${BASH_SOURCE[0]:-}" ]; then
    SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
else
    SRC_DIR="$PWD"   # stdin modu (sudo bash < scripts/install.sh): BASH_SOURCE boş → checkout=CWD (Codex :10)
fi
if [ "$SRC_DIR" = "/opt/linux-ai-server" ]; then
    echo "HATA: install.sh'ı /opt kopyasından değil, temiz bir checkout'tan koş." >&2
    exit 1
fi
# Yanlış SRC_DIR'le sessiz-yanlış kurulumdan kaçın: checkout işaretlerini doğrula.
if [ ! -d "$SRC_DIR/app" ] || [ ! -f "$SRC_DIR/scripts/setup-gh-runner.sh" ]; then
    echo "HATA: checkout kökü çözülemedi (SRC_DIR=$SRC_DIR). Repo kökünden koş." >&2
    exit 1
fi

# Check root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (sudo)"
    exit 1
fi

# Create user and group
if ! getent group aiserver > /dev/null; then
    groupadd aiserver
    echo "Created group: aiserver"
fi

if ! id aiserver > /dev/null 2>&1; then
    useradd -r -g aiserver -d /opt/linux-ai-server -s /bin/bash aiserver
    echo "Created user: aiserver"
fi

# Create directories
mkdir -p /opt/linux-ai-server
mkdir -p /var/lib/linux-ai-server
mkdir -p /var/log/linux-ai-server
mkdir -p /var/AI-stump/agents
mkdir -p /etc/linux-ai-server

# Copy files — replacement semantics (rm-rf + cp -rT): `cp -r X/ dest/` dest VARSA GNU-cp'de
# dest/X nesteler → upgrade'de stale kod kalır (Codex :28/:63). Kaynak = $SRC_DIR (checkout).
rm -rf /opt/linux-ai-server/app /opt/linux-ai-server/scripts /opt/linux-ai-server/extensions
cp -rT "$SRC_DIR/app" /opt/linux-ai-server/app
cp -rT "$SRC_DIR/scripts" /opt/linux-ai-server/scripts
cp -rT "$SRC_DIR/extensions" /opt/linux-ai-server/extensions
cp "$SRC_DIR/pyproject.toml" /opt/linux-ai-server/
# ci_fixer fail-CLOSED (#242): CI_FIXER_SETTINGS varsayılanı automation/ci-fixer-settings.json'a
# çözülür; dosya yoksa ci_fixer ABORT eder → paketli-deploy'da da gelsin.
mkdir -p /opt/linux-ai-server/automation
cp "$SRC_DIR/automation/ci-fixer-settings.json" /opt/linux-ai-server/automation/
rm -rf /etc/linux-ai-server/config
cp -r "$SRC_DIR/config" /etc/linux-ai-server/
cp "$SRC_DIR/config/env" /etc/linux-ai-server/env 2>/dev/null || true

# Install Python dependencies
cd /opt/linux-ai-server
pip install -e . --quiet

# Set permissions
chown -R aiserver:aiserver /opt/linux-ai-server
chown -R aiserver:aiserver /var/lib/linux-ai-server
chown -R aiserver:aiserver /var/log/linux-ai-server
chown -R aiserver:aiserver /var/AI-stump

# --- gh-runner: setup script + build context'i app-user'ın DOKUNAMAYACAĞI ROOT-sahipli yere kur ---
# /opt aiserver'a chown'lu olduğundan oradaki sudo/docker'lı setup script'i VE Dockerfile/
# entrypoint'i, ele geçen aiserver tarafından tamper edilip aylık owner-rebuild'de root/docker-
# exec'e çevrilebilirdi (Codex install-hardening). Çözüm: kaynağı $SRC_DIR'den (operatör-sahipli
# checkout — /opt DEĞİL, TOCTOU yok) /usr/local'e (root-sahipli, go-w'siz, taze=rm-rf ile eski
# bozuk-mode'lar sıfır) kopyala; operatör buradan koşar. /opt kopyası yalnız repo-bütünlüğü.
install -d -m 0755 -o root -g root /usr/local/lib/koken-runner
rm -rf /usr/local/lib/koken-runner/gh-runner
cp -rT "$SRC_DIR/extensions/gh-runner" /usr/local/lib/koken-runner/gh-runner
chown -R root:root /usr/local/lib/koken-runner/gh-runner
chmod -R go-w /usr/local/lib/koken-runner/gh-runner
install -m 0755 -o root -g root "$SRC_DIR/scripts/setup-gh-runner.sh" /usr/local/sbin/koken-runner-setup
echo "gh-runner kurmak için: sudo koken-runner-setup  (root-sahipli kaynak; aiserver tamper edemez)"

# Create systemd service
cat > /etc/systemd/system/linux-ai-server.service << 'UNIT'
[Unit]
Description=Linux-AI Server
After=network.target
Wants=network-online.target

[Service]
Type=exec
User=aiserver
Group=aiserver
WorkingDirectory=/opt/linux-ai-server
ExecStart=/usr/local/bin/uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8420 --workers 2
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

EnvironmentFile=-/etc/linux-ai-server/env

# Security hardening
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/linux-ai-server /var/log/linux-ai-server /var/AI-stump
PrivateTmp=yes
# UMask=0002: server.db + -wal/-shm grup-yazilabilir (664) olsun -> setgid data-dir
# + ortak-grup ile ikinci-bir-user (or. note-poller/klipper-auto emit-event.sh
# uzerinden) da yazabilsin -> SQLITE_READONLY (#517) sinifi kapali.
UMask=0002

[Install]
WantedBy=multi-user.target
UNIT

# GUVENLIK: JWT_SECRET'i her kurulumda benzersiz rastgele uret — placeholder
# ASLA shipped degil (eski 'change-me-in-production' public-default = JWT forge).
# EnvironmentFile ile yuklenir; create_app guard placeholder/bos'u reddeder.
mkdir -p /etc/linux-ai-server
if ! grep -qs '^JWT_SECRET=' /etc/linux-ai-server/env; then
    printf 'JWT_SECRET=%s\n' "$(openssl rand -hex 32)" >> /etc/linux-ai-server/env
fi
chmod 600 /etc/linux-ai-server/env

# Generate initial API key
python3 /opt/linux-ai-server/scripts/generate_api_key.py

# Codex P1: generate_api_key.py ROOT olarak calisir -> server.db'yi root:root 0644
# yaratir (line 43 chown'dan SONRA) -> aiserver servisi ilk-write'ta SQLITE_READONLY.
# Key-gen SONRASI sahiplik+grup-yaz duzelt + setgid (gelecek -wal/-shm grup-devralir;
# UMask=0002 ile birlikte ikinci-user de yazabilir, #517 sinifi kapali).
chown -R aiserver:aiserver /var/lib/linux-ai-server
chmod -R g+w /var/lib/linux-ai-server
chmod g+s /var/lib/linux-ai-server

# Enable and start
systemctl daemon-reload
systemctl enable linux-ai-server

echo ""
echo "=== Installation Complete ==="
echo "Start:   sudo systemctl start linux-ai-server"
echo "Status:  sudo systemctl status linux-ai-server"
echo "Logs:    sudo journalctl -u linux-ai-server -f"
echo "API:     http://localhost:8420/health"
echo "Swagger: http://localhost:8420/docs"
