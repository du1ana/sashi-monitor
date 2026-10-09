# sashimon

HotPocket consensus observer for Sashimono hosts. Built to see **forks, stalls, height splits and frozen nodes**
in HotPocket clusters, and to compare clusters side by side (for example stock 0.6.5 against a patched 0.6.6).

## How it works

- **Discovery:** `sashi list` every 15 s. Every running instance is followed.
- **Reading logs:** each instance's own `hp.log` file (`/home/<user>/<instance>/log/`), following plog's rotation.
  sashimon never attaches to the container. A slow reader on a container's TTY blocks hpcore's stdout and freezes
  consensus; v1 did exactly that through `sashi attach`.
- **What is stored:** no raw log lines. Each line updates per-node, per-minute counters, and a few lines become events.

  | Kind | Contents |
  |---|---|
  | Per-minute counters | closes, height, vote splits, "not enough peers", shard disagreement, desyncs, missed stages, skipped rounds, refused closes, late closes, fast-forwards, sync targets, errors, frozen seconds |
  | Ledgers | one row per (ledger number, hash), with the nodes that closed it; used for fork detection |
  | Events | fast-forwards, refused closes, config/UNL patches, invalid/conflicting certificates, node starts, errors |
  | HotPocket stats | 0.6.6+ `Consensus stats` lines |
- **Raw logs on demand:** the last ~6000 lines per node stay in memory. When an incident fires, the buffers of the
  involved nodes are saved, compressed, with the incident.
- **Incidents, over the merged view of all hosts:**

  | Incident | Trigger |
  |---|---|
  | **fork** | the same ledger number with different hashes |
  | **stall** | no ledger closed for 60 s |
  | **height split** | a live node more than 5 ledgers behind |
  | **freeze** | a node silent for 15 s; sashimon also records where its hpcore threads are blocked, and a thread inside `write()` to stdout means its output is not being drained |
- **Peers:** each sashimon polls the others (`--peer`), so every dashboard shows the whole cluster across hosts.
- **Bounded storage:**
  - batched writes, one transaction per second;
  - age retention (72 h) plus a size cap (512 MB);
  - incremental vacuum and a truncated WAL.

  A busy 18-node debug-level cluster uses a few MB per hour.

Standard library Python only. The dashboard is a Svelte app built into one self-contained `dashboard.html`.

## Dashboard

- **Clusters:** status (healthy / degraded / stalled / forked / offline), HotPocket version, height, spread between
  nodes, open incidents. Pick two to compare.
- **Cluster view:**
  - node grid (height lag, vote status, frozen)
  - ledgers per minute with incident bands
  - node × minute heatmap
  - incidents with captured logs and freeze diagnosis
  - host load
- **Compare:** throughput, stall time, forks, splits, freezes, refused closes, fast-forwards and per-node rates for
  two clusters over the same window.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/du1ana/sashi-monitor/main/install.sh | \
  sudo SASHIMON_HOST_LABEL=dev15 SASHIMON_PEERS=https://dapps-dev8.geveo.com:8765 bash
```

- Re-running the installer upgrades in place.
- The v1 database (`/var/lib/sashimon/events.db*`) is deleted unless `SASHIMON_KEEP_V1_DB=1`.
- HTTPS is used automatically with the Sashimono contract-template certificate (`SASHIMON_TLS_AUTO=0` to disable).
- `journalctl -u sashimon -f` for logs. `uninstall.sh` removes it (`PURGE=1` also deletes the data).

Main options (flag / env):

| Flag | Env | Default | |
|---|---|---|---|
| `--peer URL` (repeatable) | `SASHIMON_PEERS` (comma-separated) | none | other hosts' sashimon |
| `--host-label` | `SASHIMON_HOST_LABEL` | hostname | name shown for this host |
| `--retention-hours` | `SASHIMON_RETENTION_HOURS` | 72 | |
| `--max-db-mb` | `SASHIMON_MAX_DB_MB` | 512 | oldest log captures are dropped first |
| `--freeze-seconds` | `SASHIMON_FREEZE_S` | 15 | |
| `--stall-seconds` | `SASHIMON_STALL_S` | 60 | |
| `--split-ledgers` | `SASHIMON_SPLIT_LEDGERS` | 5 | |
| `--ring-lines` | `SASHIMON_RING_LINES` | 6000 | per node |
| `--instances-dir GLOB` | `SASHIMON_INSTANCES_DIR` | | test mode: node directories (each with `cfg/hp.cfg`, `log/hp.log`) instead of `sashi list` |

## API

| Endpoint | |
|---|---|
| `GET /api/overview` | hosts, clusters (live, plus offline ones still in the DB), open incidents |
| `GET /api/cluster?contract_id=&window=` | live nodes (all hosts), per-host history, recent ledger hashes |
| `GET /api/incident?host=&id=` | incident detail with log captures and process diagnosis |
| `GET /api/local/live`, `/api/local/history`, `/api/local/incident` | per-host data, used between peers |
| `POST /api/clear` | wipe stored history |

## Development

```bash
cd frontend && npm install
npm run dev     # proxies /api to SASHIMON_URL (default http://127.0.0.1:18765)
npm run build   # writes ../dashboard.html
```

A local test setup with hpdevkit (two monitors federating over a 5-node cluster, plus scenario drivers) lives in the
consensus investigation repo (`fix-tests/sashimon_localtest.py`).
