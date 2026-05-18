#!/bin/bash
# One-shot installer for the v2 (kernel-inhibit) posture-aware input service.
# Run with: sudo bash install.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "This script must be run as root. Try: sudo bash $0" >&2
  exit 1
fi

install -m 755 "$HERE/surface-posture-input.py" /usr/local/bin/surface-posture-input
install -m 644 "$HERE/surface-posture-input.service" /etc/systemd/system/surface-posture-input.service
systemctl daemon-reload
systemctl enable surface-posture-input.service
systemctl restart surface-posture-input.service
sleep 1
systemctl --no-pager status surface-posture-input.service | head -20
echo
echo "Installed. Tail live with:"
echo "  journalctl -u surface-posture-input.service -f"
