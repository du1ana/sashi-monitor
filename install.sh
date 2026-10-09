#!/usr/bin/env bash
# sashimon installer (v2). Run as root on a Sashimono host.
#
# One-liner:
#   curl -fsSL https://raw.githubusercontent.com/du1ana/sashi-monitor/main/install.sh | sudo bash
#
# With peers (other hosts running sashimon, so every dashboard shows the whole cluster):
#   curl -fsSL https://raw.githubusercontent.com/du1ana/sashi-monitor/main/install.sh | \
#     sudo SASHIMON_PEERS=https://dapps-dev8.geveo.com:8765 SASHIMON_HOST_LABEL=dev15 bash
#
# Env overrides: SASHIMON_REPO, SASHIMON_PORT (8765), SASHIMON_BIND (0.0.0.0), SASHIMON_PEERS (comma-separated URLs),
# SASHIMON_HOST_LABEL (hostname), SASHIMON_RETENTION_HOURS (72), SASHIMON_MAX_DB_MB (512), SASHIMON_TLS_AUTO (1),
# SASHIMON_TLS_CERT / SASHIMON_TLS_KEY, SASHIMON_KEEP_V1_DB (0: the old v1 events.db is deleted).

set -euo pipefail

REPO="${SASHIMON_REPO:-https://raw.githubusercontent.com/du1ana/sashi-monitor/main}"
INSTALL_DIR="${SASHIMON_INSTALL_DIR:-/opt/sashimon}"
DATA_DIR="${SASHIMON_DATA_DIR:-/var/lib/sashimon}"
PORT="${SASHIMON_PORT:-8765}"
BIND="${SASHIMON_BIND:-0.0.0.0}"
PEERS="${SASHIMON_PEERS:-}"
HOST_LABEL="${SASHIMON_HOST_LABEL:-$(hostname)}"
RETENTION_HOURS="${SASHIMON_RETENTION_HOURS:-72}"
MAX_DB_MB="${SASHIMON_MAX_DB_MB:-512}"
TLS_AUTO="${SASHIMON_TLS_AUTO:-1}"
TLS_CERT="${SASHIMON_TLS_CERT:-}"
TLS_KEY="${SASHIMON_TLS_KEY:-}"
KEEP_V1_DB="${SASHIMON_KEEP_V1_DB:-0}"
SERVICE_FILE="/etc/systemd/system/sashimon.service"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (use sudo)." >&2
  exit 1
fi

PY="$(command -v python3 || true)"
if [[ -z "$PY" ]]; then
  echo "[sashimon] installing python3"
  if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get update -y && apt-get install -y --no-install-recommends python3 ca-certificates curl
  else
    echo "[sashimon] python3 not found and no apt-get; install python3 >= 3.8 first" >&2
    exit 1
  fi
  PY="$(command -v python3)"
fi
command -v sashi >/dev/null 2>&1 || echo "[sashimon] WARNING: 'sashi' not found; no instances will be discovered." >&2

echo "[sashimon] installing to $INSTALL_DIR  data=$DATA_DIR  port=$PORT  host=$HOST_LABEL  peers=${PEERS:-none}"

systemctl stop sashimon 2>/dev/null || true

mkdir -p "$INSTALL_DIR" "$DATA_DIR"
curl -fsSL "$REPO/sashimon.py" -o "$INSTALL_DIR/sashimon.py.new"
curl -fsSL "$REPO/dashboard.html" -o "$INSTALL_DIR/dashboard.html.new"
mv "$INSTALL_DIR/sashimon.py.new" "$INSTALL_DIR/sashimon.py"
mv "$INSTALL_DIR/dashboard.html.new" "$INSTALL_DIR/dashboard.html"
chmod +x "$INSTALL_DIR/sashimon.py"
rm -f "$INSTALL_DIR/index.html"      # v1 leftover

# v1 stored every log line in events.db (gigabytes at debug level). v2 uses sashimon.db.
if ls "$DATA_DIR"/events.db* >/dev/null 2>&1; then
  if [[ "$KEEP_V1_DB" == "1" ]]; then
    echo "[sashimon] keeping v1 database $DATA_DIR/events.db (SASHIMON_KEEP_V1_DB=1)"
  else
    echo "[sashimon] deleting v1 database: $(du -sh "$DATA_DIR" | cut -f1) in $DATA_DIR/events.db*"
    rm -f "$DATA_DIR"/events.db "$DATA_DIR"/events.db-wal "$DATA_DIR"/events.db-shm
  fi
fi

FLAGS="--db $DATA_DIR/sashimon.db --port $PORT --bind $BIND --host-label $HOST_LABEL"
FLAGS="$FLAGS --retention-hours $RETENTION_HOURS --max-db-mb $MAX_DB_MB"
IFS=',' read -r -a PEER_LIST <<< "$PEERS"
for p in "${PEER_LIST[@]}"; do
  [[ -n "$p" ]] && FLAGS="$FLAGS --peer $p"
done
if [[ -n "$TLS_CERT" && -n "$TLS_KEY" ]]; then
  FLAGS="$FLAGS --tls-cert $TLS_CERT --tls-key $TLS_KEY"
elif [[ "$TLS_AUTO" != "0" && "$TLS_AUTO" != "false" ]]; then
  FLAGS="$FLAGS --tls-auto"
fi

cat >"$SERVICE_FILE" <<UNIT
[Unit]
Description=sashimon - HotPocket consensus observer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PY $INSTALL_DIR/sashimon.py $FLAGS
Restart=always
RestartSec=5
User=root
Nice=10
IOSchedulingClass=idle
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | head -n1 | grep -qi active; then
  ufw allow "${PORT}/tcp" >/dev/null 2>&1 || true
fi

systemctl daemon-reload
systemctl enable sashimon >/dev/null 2>&1 || true
systemctl restart sashimon
sleep 2
if systemctl is-active --quiet sashimon; then
  echo "[sashimon] running: $(journalctl -u sashimon -n 1 --no-pager -o cat)"
else
  echo "[sashimon] service failed to start: journalctl -u sashimon --no-pager -n 50" >&2
  exit 1
fi
