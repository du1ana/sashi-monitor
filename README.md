# sashimon

Sashimono HotPocket instance status monitor.

Tails every Sashimono instance on the host via `sashi list` + `sashi attach`,
classifies each log line, persists to SQLite, and serves a small live
dashboard with per-instance health, ledger uptime, and stacked-bar event
timelines.

Designed to drop onto any Sashimono host with one curl-and-pipe.

## What it watches

Discovery: `sashi list` (every 30s — picks up newly created instances).

Per-instance log stream: `sashi attach -n <name>` over an allocated PTY so
sashi treats it as interactive.

Each line is classified (regex sourced from `hpcore/src/consensus.cpp`):

| Tag                | Match                                                                |
|--------------------|----------------------------------------------------------------------|
| `ledger_created`   | `****Ledger created****` — healthy round                             |
| `contract_running` | `contract is running`                                                |
| `consensus_lost`   | `Not enough peers proposing to perform consensus`                    |
| `fork_warn`        | `Cannot close ledger. Possible fork condition` / shard-hash variant  |
| `out_of_sync`      | `We are not on the consensus ledger`                                 |
| `error`            | `[err]`, usage-limit failure, contract execution failure, etc.       |
| `warning`          | `[wrn]`, output-hash mismatch, missing candidate input               |
| `role_change`      | observer / validator switch                                          |
| `hp_started` / `hp_stopped` | consensus processor lifecycle                              |
| `info_other`       | everything else                                                      |

Health is a coarse derived state per instance:

- **healthy** — produced a `ledger_created` within the last 30s
- **consensus_loss** — recent `consensus_lost` and no recent ledger
- **forked** — any `fork_warn` in window
- **stalled** — no events for 30s+
- **unknown** — not enough data yet

## Install

One-liner (run on the Sashimono host as root):

```bash
curl -fsSL https://raw.githubusercontent.com/du1ana/sashi-monitor/main/install.sh | sudo bash
```

Override defaults via env:

```bash
SASHIMON_PORT=9000 SASHIMON_BIND=127.0.0.1 \
SASHIMON_REPO=https://raw.githubusercontent.com/du1ana/sashi-monitor/main \
curl -fsSL "$SASHIMON_REPO/install.sh" | sudo -E bash
```

After install:

```
journalctl -u sashimon -f          # live service logs
systemctl status sashimon
http://<host-ip>:8765               # dashboard
```

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/du1ana/sashi-monitor/main/uninstall.sh | sudo bash
# add PURGE=1 to also delete /var/lib/sashimon
```

## Manual run (no systemd)

```bash
sudo python3 /opt/sashimon/sashimon.py \
  --db /var/lib/sashimon/events.db \
  --port 8765 --bind 0.0.0.0
```

## Env / flags

| Flag                | Env                          | Default                       |
|---------------------|------------------------------|-------------------------------|
| `--db`              | `SASHIMON_DB`                | `/var/lib/sashimon/events.db` |
| `--port`            | `SASHIMON_PORT`              | `8765`                        |
| `--bind`            | `SASHIMON_BIND`              | `0.0.0.0`                     |
| `--sashi`           | `SASHIMON_SASHI`             | `sashi` (resolved via PATH)   |
| `--retention-days`  | `SASHIMON_RETENTION_DAYS`    | `14`                          |

## HTTP API

| Path                                                     | Returns                                           |
|----------------------------------------------------------|---------------------------------------------------|
| `GET /`                                                  | embedded dashboard                                |
| `GET /api/instances`                                     | last-known `sashi list` rows                      |
| `GET /api/summary?window=3600`                           | per-instance health + tag counts in window        |
| `GET /api/histogram?window=3600&bucket=60[&instance=X]`  | tag counts per time bucket (for charts)           |
| `GET /api/events?instance=X&since=<ts>&until=<ts>&tag=Y` | raw event rows                                    |
| `GET /healthz`                                           | `{"ok":true}`                                     |

`since`/`until` are POSIX seconds.

## Requirements

- Linux host (uses `pty`, systemd).
- Python 3.10+ (stdlib only — no pip deps).
- `sashi` CLI in PATH; service runs as root because `sashi attach` needs it.
- Outbound 443 to `cdn.jsdelivr.net` from whoever opens the dashboard
  (Chart.js is loaded from CDN). For air-gapped hosts, vendor it locally.

## Storage

Single SQLite file. WAL-mode, two indexed tables (`events`, `instances`).
Old events pruned every hour past `--retention-days`.

Rough sizing: a healthy instance emits ~30 ledger events/min. 10 instances ×
14 days ≈ 6M rows ≈ 600 MB. Tune with `SASHIMON_RETENTION_DAYS`.

## Troubleshooting

**Empty dashboard / "No Sashimono instances detected yet"**
- `sashi list` from the same shell as the service. If empty, sashi has no
  instances. If permission denied, ensure the service is `User=root`.

**`sashi attach` exits immediately**
- sashimon retries with exponential backoff (1s → 30s). Check
  `journalctl -u sashimon` for the per-tail error.
- Some sashi versions require a TTY. The daemon already allocates a PTY via
  `pty.openpty()`; if your build still refuses, run sashimon under `script -q`.

**Wrong timestamps**
- HotPocket prints UTC-ish timestamps. Sashimon parses them as UTC. The
  dashboard renders in browser locale.

**Service won't bind**
- Port 8765 in use? Override `SASHIMON_PORT` and reinstall, or
  `systemctl edit sashimon` to change `ExecStart`.

## Files

```
/opt/sashimon/sashimon.py           # daemon
/etc/systemd/system/sashimon.service
/var/lib/sashimon/events.db         # SQLite store
```

## License

MIT (or match parent repo).
