#!/usr/bin/env bash
# Install the Shabbat Elevator Detector as a systemd service on an RPi.
# Run as root: sudo bash install.sh
# Assumes the project is already cloned to /home/eco/elevator-RFID

set -euo pipefail

PROJECT_DIR="/home/eco/elevator-RFID"
VENV_DIR="$PROJECT_DIR/venv"
SERVICE_NAME="shabbat-detector"
STATE_DIR="/var/lib/shabbat_detector"
SERVICE_SRC="$PROJECT_DIR/shabbat_detector/shabbat-detector.service"
SERVICE_DEST="/etc/systemd/system/$SERVICE_NAME.service"

echo "=== Shabbat Detector installer ==="

# 1. Create virtualenv and install deps
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtualenv at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi

echo "Installing Python dependencies..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet requests sseclient-py pyserial

# 2. Create state dir
echo "Creating state dir $STATE_DIR"
mkdir -p "$STATE_DIR"
chown eco:eco "$STATE_DIR"

# 3. Install systemd unit
echo "Installing $SERVICE_DEST"
cp "$SERVICE_SRC" "$SERVICE_DEST"
chmod 644 "$SERVICE_DEST"

# 4. Reload systemd, enable and start
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo ""
echo "Done. Service status:"
systemctl status "$SERVICE_NAME" --no-pager -l
echo ""
echo "Live logs: journalctl -u $SERVICE_NAME -f"
