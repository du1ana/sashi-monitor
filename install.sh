#!/usr/bin/env bash
# Sashimon installer. Run as root.
#
# One-liner:
#   curl -fsSL https://raw.githubusercontent.com/du1ana/sashi-monitor/main/install.sh | sudo bash
#
# Override repo via env:
#   SASHIMON_REPO=https://raw.githubusercontent.com/foo/bar/main \
#   SASHIMON_PORT=9000 SASHIMON_BIND=127.0.0.1 \
#   curl -fsSL "$SASHIMON_REPO/install.sh" | sudo -E bash

set -euo pipefail

REPO="${SASHIMON_REPO:-https://raw.githubusercontent.com/du1ana/sashi-monitor/main}"

INSTALL_DIR="${SASHIMON_INSTALL_DIR:-/opt/sashimon}"
DATA_DIR="${SASHIMON_DATA_DIR:-/var/lib/sashimon}"
PORT="${SASHIMON_PORT:-8765}"
BIND="${SASHIMON_BIND:-0.0.0.0}"
SERVICE_FILE="/etc/systemd/system/sashimon.service"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (use sudo)." >&2
  exit 1
fi

echo "[sashimon] installing to $INSTALL_DIR  data=$DATA_DIR  port=$PORT"

# 1. Deps
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y --no-install-recommends python3 ca-certificates curl
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 ca-certificates curl
elif command -v yum >/dev/null 2>&1; then
  yum install -y python3 ca-certificates curl
else
  echo "[sashimon] no supported package manager; ensure python3 is installed" >&2
fi

PY="$(command -v python3)"
if [[ -z "$PY" ]]; then
  echo "[sashimon] python3 not found" >&2
  exit 1
fi

# 2. Sanity-check sashi
if ! command -v sashi >/dev/null 2>&1; then
  echo "[sashimon] WARNING: 'sashi' binary not found in PATH." >&2
  echo "             Sashimon will start but won't see instances until 'sashi' is installed." >&2
fi

# 3. Fetch payload
mkdir -p "$INSTALL_DIR" "$DATA_DIR"
echo "[sashimon] downloading sashimon.py"
curl -fsSL "$REPO/sashimon.py" -o "$INSTALL_DIR/sashimon.py"
chmod +x "$INSTALL_DIR/sashimon.py"

# 4. systemd unit
echo "[sashimon] writing $SERVICE_FILE"
cat >"$SERVICE_FILE" <<UNIT
[Unit]
Description=Sashimono HotPocket instance monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PY $INSTALL_DIR/sashimon.py --db $DATA_DIR/events.db --port $PORT --bind $BIND
Restart=always
RestartSec=5
User=root
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

# 5. Enable + start
systemctl daemon-reload
systemctl enable sashimon >/dev/null 2>&1 || true
systemctl restart sashimon

sleep 1
if systemctl is-active --quiet sashimon; then
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [[ -z "$IP" ]] && IP="127.0.0.1"
  echo
  echo "[sashimon] running. dashboard: http://$IP:$PORT"
  echo "[sashimon] logs: journalctl -u sashimon -f"
else
  echo "[sashimon] service failed to start. journalctl -u sashimon --no-pager -n 50" >&2
  exit 1
fi
