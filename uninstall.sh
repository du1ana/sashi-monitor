#!/usr/bin/env bash
# Sashimon uninstaller. Run as root.
#   curl -fsSL https://raw.githubusercontent.com/du1ana/sashi-monitor/main/uninstall.sh | sudo bash
#   PURGE=1 ... bash    # also delete /var/lib/sashimon (events.db)

set -euo pipefail

INSTALL_DIR="${SASHIMON_INSTALL_DIR:-/opt/sashimon}"
DATA_DIR="${SASHIMON_DATA_DIR:-/var/lib/sashimon}"
SERVICE_FILE="/etc/systemd/system/sashimon.service"
PURGE="${PURGE:-0}"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (use sudo)." >&2
  exit 1
fi

systemctl stop sashimon 2>/dev/null || true
systemctl disable sashimon 2>/dev/null || true
rm -f "$SERVICE_FILE"
systemctl daemon-reload

rm -rf "$INSTALL_DIR"

if [[ "$PURGE" == "1" ]]; then
  rm -rf "$DATA_DIR"
  echo "[sashimon] purged $DATA_DIR"
else
  echo "[sashimon] data kept at $DATA_DIR (set PURGE=1 to remove)"
fi

echo "[sashimon] uninstalled"
