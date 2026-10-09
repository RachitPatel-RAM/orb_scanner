#!/usr/bin/env bash
# ==============================================================================
# Google Cloud / Linux 24/7 Deployment Script for BornBull Trading Desk
# ==============================================================================

set -e

echo "🚀 Setting up BornBull 24/7 Scanner on Google Cloud..."

# Update and install python
sudo apt-get update
sudo apt-get install -y python3 python3-pip python3-venv git curl

# Set system timezone to IST
sudo timedatectl set-timezone Asia/Kolkata

APP_DIR="/opt/orb_scanner"
sudo mkdir -p $APP_DIR
sudo chown -R $USER:$USER $APP_DIR

# Clone / sync repository
if [ -d "$APP_DIR/.git" ]; then
    cd $APP_DIR
    git pull
else
    cd /opt
    git clone https://github.com/RachitPatel-RAM/orb_scanner.git
    cd $APP_DIR
fi

# Setup Virtual Environment
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Create systemd service for 24/7 auto-restart
sudo bash -c "cat > /etc/systemd/system/orb_scanner.service << 'EOF'
[Unit]
Description=BornBull Trading Desk 24/7 Auto-Scanner
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python main.py live
Restart=always
RestartSec=10
EnvironmentFile=$APP_DIR/.env

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
