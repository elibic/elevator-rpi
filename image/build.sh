#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
GENERATOR="${1:?Pass the path to rpi-image-gen v2.9.0}"
test "$(git -C "$GENERATOR" rev-parse HEAD)" = d2d5705bcb37284c4b08c0ce43c039b1f1004e25
[[ "${CONTROLLER_ADMIN_PUBLIC_KEY:-}" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+(\ [^$'\r\n']*)?$ ]] || {
  echo 'Set CONTROLLER_ADMIN_PUBLIC_KEY to the maintenance SSH public key.' >&2
  exit 1
}
read -r ADMIN_KEY_TYPE ADMIN_KEY_DATA _ <<< "$CONTROLLER_ADMIN_PUBLIC_KEY"
ADMIN_KEY_FILE="$(mktemp)"
trap 'rm -f "$ADMIN_KEY_FILE"' EXIT
printf '%s %s\n' "$ADMIN_KEY_TYPE" "$ADMIN_KEY_DATA" > "$ADMIN_KEY_FILE"
git -C "$REPO" archive HEAD VERSION setup.sh elevator_tracker_rfid.py monitor.py installer systemd shabbat_detector provisioning > "$HERE/payload.tar"
chmod +x "$HERE/hooks/customize90-controller"
cd "$GENERATOR"
./rpi-image-gen build -S "$HERE" -c "$HERE/config.yaml" -- "IGconf_ssh_pubkey_user1=$ADMIN_KEY_FILE"
