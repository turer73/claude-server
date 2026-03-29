#!/bin/bash
set -e

echo "=== Linux-AI Server Installer ==="

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

# Copy files
cp -r app/ /opt/linux-ai-server/
cp pyproject.toml /opt/linux-ai-server/
cp -r config/ /etc/linux-ai-server/

# Install Python dependencies
cd /opt/linux-ai-server
pip install -e . --quiet

# Set permissions
chown -R aiserver:aiserver /opt/linux-ai-server
chown -R aiserver:aiserver /var/lib/linux-ai-server
chown -R aiserver:aiserver /var/log/linux-ai-server
chown -R aiserver:aiserver /var/AI-stump

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

Environment=JWT_SECRET=change-me-in-production
EnvironmentFile=-/etc/linux-ai-server/env

# Security hardening
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/linux-ai-server /var/log/linux-ai-server /var/AI-stump
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
UNIT

# Generate initial API key
python3 /opt/linux-ai-server/scripts/generate_api_key.py

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
