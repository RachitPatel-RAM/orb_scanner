#!/usr/bin/env bash
# ==============================================================================
# Google Cloud / Linux 24/7 Deployment Script for BornBull Trading Desk
# ==============================================================================

set -e

echo "🚀 Setting up BornBull 24/7 Scanner on Google Cloud..."

# Set system timezone to IST
sudo timedatectl set-timezone Asia/Kolkata || true

APP_DIR="$(pwd)"
CURRENT_USER="$USER"

# Setup Virtual Environment if not exists
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Create systemd service for 24/7 auto-restart with real expanded variables
sudo bash -c "cat > /etc/systemd/system/orb_scanner.service << EOF
[Unit]
Description=BornBull Trading Desk 24/7 Auto-Scanner
After=network.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/python main.py live
Restart=always
RestartSec=10
EnvironmentFile=-${APP_DIR}/.env

[Install]
WantedBy=multi-user.target
EOF"

# Enable and start service
sudo systemctl daemon-reload
sudo systemctl enable orb_scanner
sudo systemctl restart orb_scanner

echo "✅ BornBull Scanner is now running 24/7 under systemd!"
echo "Check status with: sudo systemctl status orb_scanner"
echo "View live logs with: sudo journalctl -u orb_scanner -f"
