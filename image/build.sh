#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
GENERATOR="${1:?Pass the path to rpi-image-gen v2.9.0}"
test "$(git -C "$GENERATOR" rev-parse HEAD)" = d2d5705bcb37284c4b08c0ce43c039b1f1004e25
git -C "$REPO" archive HEAD VERSION setup.sh elevator_tracker_rfid.py monitor.py installer systemd shabbat_detector provisioning > "$HERE/payload.tar"
chmod +x "$HERE/hooks/customize90-controller"
cd "$GENERATOR"
./rpi-image-gen build -S "$HERE" -c "$HERE/config.yaml"
