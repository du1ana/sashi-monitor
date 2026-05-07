#!/usr/bin/env python3
"""
Sashimono HotPocket instance monitor.

Discovers Sashimono instances on the local VM via `sashi list`, tails each
instance's log stream via `sashi attach`, classifies events (functional /
consensus_loss / fork / error), persists to SQLite, and exposes a small
embedded HTTP dashboard.

Designed to run as root via systemd. No external Python deps required;
stdlib only.
"""

import argparse
import json
import os
import pty
import re
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

DEFAULT_DB = "/var/lib/sashimon/events.db"
DEFAULT_PORT = 8765
DEFAULT_BIND = "0.0.0.0"
DISCOVER_INTERVAL = 30        # seconds between `sashi list` polls
STALL_THRESHOLD = 30          # seconds without ledger_created => "stalled"
RETENTION_DAYS = 14           # event row retention
RETENTION_SWEEP = 3600        # seconds between retention sweeps

# --------------------------------------------------------------------------
# Log parsing
# --------------------------------------------------------------------------
#
# HotPocket log format:  YYYYMMDD HH:MM:SS.mmm [level][module] message
# Example:               20260506 14:41:10.244 [inf][hpc] ****Ledger created**** ...
#
# Lines that don't match (e.g. contract-emitted "DOVA contract is running...")
# are still captured but timestamped at receipt time.

LOG_RE = re.compile(
    r"^(?P<date>\d{8})\s+(?P<time>\d{2}:\d{2}:\d{2}\.\d{3})\s+"
    r"\[(?P<level>\w+)\]\[(?P<module>\w+)\]\s+(?P<msg>.*)$"
)

# Order matters: first match wins. Most specific first.
CLASSIFIERS = (
    ("ledger_created",   re.compile(r"\*+\s*Ledger created\s*\*+", re.I)),
    ("fork_warn",        re.compile(
        r"(Cannot close ledger.*fork condition|"
        r"No consensus on last shard hash.*fork condition)", re.I)),
    ("consensus_lost",   re.compile(r"Not enough peers proposing to perform consensus", re.I)),
    ("out_of_sync",      re.compile(r"We are not on the consensus ledger", re.I)),
    ("contract_running", re.compile(r"contract is running", re.I)),
    ("role_change",      re.compile(r"Switched (?:to OBSERVER|back to VALIDATOR) mode", re.I)),
    ("hp_started",       re.compile(r"^HotPocket\s+\d|Consensus processor started", re.I)),
    ("hp_stopped",       re.compile(r"Consensus processor stopped", re.I)),
    ("error",            re.compile(
        r"\[err\]|HotPocket usage limit failure|Consensus thread exited|"
        r"Error occured when closing ledger|contract execution failed|"
        r"patch file changes after consensus failed", re.I)),
    ("warning",          re.compile(
        r"\[wrn\]|Consensus output hash didn't match|"
        r"Input required but wasn't in our candidate inputs", re.I)),
)


def classify(msg: str) -> str:
    for tag, rx in CLASSIFIERS:
        if rx.search(msg):
            return tag
    return "info_other"


def parse_log_line(line: str, now_ts: float) -> dict | None:
    line = line.rstrip("\r\n")
    if not line.strip():
        return None
    m = LOG_RE.match(line)
    if m:
        try:
            ts = datetime.strptime(
                m.group("date") + m.group("time"),
                "%Y%m%d%H:%M:%S.%f",
            ).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            ts = now_ts
        msg = m.group("msg")
        return {
            "ts": ts,
            "level": m.group("level"),
            "module": m.group("module"),
            "tag": classify(msg),
            "msg": msg,
        }
    # Free-form line (contract stdout, etc.)
    return {
        "ts": now_ts,
        "level": "raw",
        "module": "contract",
        "tag": classify(line),
        "msg": line,
    }


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    instance  TEXT    NOT NULL,
    ts        REAL    NOT NULL,
    level     TEXT,
    module    TEXT,
    tag       TEXT,
    msg       TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_instance_ts ON events(instance, ts);
CREATE INDEX IF NOT EXISTS idx_events_tag         ON events(tag);
CREATE INDEX IF NOT EXISTS idx_events_ts          ON events(ts);

CREATE TABLE IF NOT EXISTS instances (
    name         TEXT PRIMARY KEY,
    contract_id  TEXT,
    tenant       TEXT,
    image        TEXT,
    user_port    INTEGER,
    peer_port    INTEGER,
    status       TEXT,
    first_seen   REAL,
    last_seen    REAL
);
"""


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=10.0)
        self.conn.executescript(SCHEMA)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.commit()

    def insert_event(self, instance: str, ev: dict) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO events (instance, ts, level, module, tag, msg) "
                "VALUES (?,?,?,?,?,?)",
                (instance, ev["ts"], ev["level"], ev["module"], ev["tag"], ev["msg"]),
            )
            self.conn.commit()

    def upsert_instance(self, info: dict) -> None:
        now = time.time()
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO instances
                  (name, contract_id, tenant, image, user_port, peer_port,
                   status, first_seen, last_seen)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET
                  contract_id = excluded.contract_id,
                  tenant      = excluded.tenant,
                  image       = excluded.image,
                  user_port   = excluded.user_port,
                  peer_port   = excluded.peer_port,
                  status      = excluded.status,
                  last_seen   = excluded.last_seen
                """,
                (
                    info["name"],
                    info.get("contract_id"),
                    info.get("tenant"),
                    info.get("image"),
                    info.get("user_port"),
                    info.get("peer_port"),
                    info.get("status"),
                    now,
                    now,
                ),
            )
            self.conn.commit()

    def list_instances(self) -> list[dict]:
        with self.lock:
            cur = self.conn.execute("SELECT * FROM instances ORDER BY name")
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def events_window(
        self,
        instance: str | None = None,
        since: float | None = None,
        until: float | None = None,
        tag: str | None = None,
        limit: int = 5000,
    ) -> list[dict]:
        q = ("SELECT instance, ts, level, module, tag, msg "
             "FROM events WHERE 1=1")
        args: list = []
        if instance:
            q += " AND instance=?"; args.append(instance)
        if since is not None:
            q += " AND ts>=?";       args.append(since)
        if until is not None:
            q += " AND ts<=?";       args.append(until)
        if tag:
            q += " AND tag=?";       args.append(tag)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            cur = self.conn.execute(q, args)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def spells(
        self,
        instance: str | None = None,
        since: float = 0.0,
        until: float | None = None,
        tags: tuple[str, ...] = (
            "consensus_lost", "fork_warn", "out_of_sync", "error", "warning",
        ),
        max_gap: float = 10.0,
        min_count: int = 1,
    ) -> list[dict]:
        """
        Group consecutive same-(instance, tag) events into spells.

        A spell ends when:
          * the next event has a different (instance, tag), or
          * the gap to the next event exceeds max_gap seconds.

        Returns each spell as:
          {instance, tag, start_ts, end_ts, duration_s, count}

        max_gap default 10s ≈ 5× HotPocket roundtime; suits consensus-loop
        emissions which repeat every ~2s while a condition holds.
        """
        if until is None:
            until = time.time()
        if not tags:
            return []
        placeholders = ",".join(["?"] * len(tags))
        q = (
            f"SELECT instance, tag, ts FROM events "
            f"WHERE tag IN ({placeholders}) AND ts>=? AND ts<=? "
        )
        args: list = list(tags) + [since, until]
        if instance:
            q += "AND instance=? "
            args.append(instance)
        q += "ORDER BY instance, tag, ts"

        out: list[dict] = []
        cur_inst: str | None = None
        cur_tag: str | None = None
        cur_start = cur_end = 0.0
        cur_count = 0

        def flush():
            if cur_inst is not None and cur_count >= min_count:
                out.append({
                    "instance":   cur_inst,
                    "tag":        cur_tag,
                    "start_ts":   cur_start,
                    "end_ts":     cur_end,
                    "duration_s": max(0.0, cur_end - cur_start),
                    "count":      cur_count,
                })

        with self.lock:
            for inst, tag, ts in self.conn.execute(q, args):
                if (inst != cur_inst or tag != cur_tag
                        or (cur_count > 0 and ts - cur_end > max_gap)):
                    flush()
                    cur_inst, cur_tag = inst, tag
                    cur_start = ts
                    cur_count = 0
                cur_end = ts
                cur_count += 1
            flush()

        out.sort(key=lambda s: s["duration_s"], reverse=True)
        return out

    def earliest_event_ts(self, instance: str | None = None) -> float | None:
        q = "SELECT MIN(ts) FROM events"
        args: list = []
        if instance:
            q += " WHERE instance=?"
            args.append(instance)
        with self.lock:
            return self.conn.execute(q, args).fetchone()[0]

    def histogram(
        self,
        instance: str | None,
        since: float,
        until: float,
        bucket_seconds: int,
    ) -> list[dict]:
        """Tag counts per time bucket for charting."""
        q = (
            "SELECT CAST((ts - ?) / ? AS INTEGER) AS bkt, tag, COUNT(*) "
            "FROM events WHERE ts>=? AND ts<=? "
        )
        args: list = [since, bucket_seconds, since, until]
        if instance:
            q += "AND instance=? "
            args.append(instance)
        q += "GROUP BY bkt, tag ORDER BY bkt"
        with self.lock:
            rows = self.conn.execute(q, args).fetchall()
        out: dict[int, dict] = {}
        for bkt, tag, cnt in rows:
            slot = out.setdefault(int(bkt), {"bucket_start": since + int(bkt) * bucket_seconds})
            slot[tag] = cnt
        return [out[k] for k in sorted(out)]

    def summary(self, window_seconds: int = 3600) -> list[dict]:
        """window_seconds=0 means all-time."""
        now = time.time()
        all_time = (window_seconds <= 0)
        since = 0.0 if all_time else now - window_seconds
        results: list[dict] = []
        with self.lock:
            inst_rows = self.conn.execute(
                "SELECT name, status FROM instances ORDER BY name"
            ).fetchall()
            for name, sashi_status in inst_rows:
                cnt_rows = self.conn.execute(
                    "SELECT tag, COUNT(*) FROM events "
                    "WHERE instance=? AND ts>=? GROUP BY tag",
                    (name, since),
                ).fetchall()
                counts = {tag: c for tag, c in cnt_rows}

                last_ledger = self.conn.execute(
                    "SELECT MAX(ts) FROM events "
                    "WHERE instance=? AND tag='ledger_created'",
                    (name,),
                ).fetchone()[0]
                last_event = self.conn.execute(
                    "SELECT MAX(ts) FROM events WHERE instance=?", (name,)
                ).fetchone()[0]

                ledger_age = (now - last_ledger) if last_ledger else None
                event_age = (now - last_event) if last_event else None

                if counts.get("fork_warn", 0) > 0:
                    health = "forked"
                elif (counts.get("consensus_lost", 0) > 0
                      and (ledger_age is None or ledger_age > STALL_THRESHOLD)):
                    health = "consensus_loss"
                elif ledger_age is not None and ledger_age <= STALL_THRESHOLD:
                    health = "healthy"
                elif event_age is not None and event_age > STALL_THRESHOLD:
                    health = "stalled"
                else:
                    health = "unknown"

                # Approx uptime % = fraction of 2s slots producing a ledger
                # (HP roundtime is 2000ms by default). For all-time, use the
                # span between first-seen and now.
                if all_time:
                    first_ts = self.conn.execute(
                        "SELECT MIN(ts) FROM events WHERE instance=?", (name,)
                    ).fetchone()[0]
                    span = max(2.0, (now - first_ts)) if first_ts else 2.0
                else:
                    span = max(2.0, float(window_seconds))
                slots = max(1, int(span // 2))
                uptime_pct = round(
                    100.0 * min(counts.get("ledger_created", 0), slots) / slots,
                    1,
                )

                results.append({
                    "name": name,
                    "sashi_status": sashi_status,
                    "health": health,
                    "last_ledger_age_s": ledger_age,
                    "last_event_age_s": event_age,
                    "uptime_pct": uptime_pct,
                    "window_s": window_seconds,
                    "counts": counts,
                })
        return results

    def prune(self, retention_days: int) -> int:
        cutoff = time.time() - retention_days * 86400
        with self.lock:
            cur = self.conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self.conn.commit()
            return cur.rowcount


# --------------------------------------------------------------------------
# Tail worker (one per instance)
# --------------------------------------------------------------------------

class Tail(threading.Thread):
    def __init__(self, instance: str, store: Store, stop_event: threading.Event,
                 sashi_bin: str):
        super().__init__(daemon=True, name=f"tail-{instance[:12]}")
        self.instance = instance
        self.store = store
        self.stop_event = stop_event
        self.sashi_bin = sashi_bin
        self.proc: subprocess.Popen | None = None
        self.master_fd: int | None = None

    def _start_proc(self) -> None:
        master, slave = pty.openpty()
        self.master_fd = master
        self.proc = subprocess.Popen(
            [self.sashi_bin, "attach", "-n", self.instance],
            stdin=slave, stdout=slave, stderr=slave,
            close_fds=True, preexec_fn=os.setsid,
        )
        os.close(slave)

    def run(self) -> None:
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                self._start_proc()
                buf = b""
                backoff = 1.0
                while not self.stop_event.is_set():
                    rlist, _, _ = select.select([self.master_fd], [], [], 1.0)
                    if not rlist:
                        if self.proc.poll() is not None:
                            break
                        continue
                    try:
                        chunk = os.read(self.master_fd, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        text = line.decode("utf-8", errors="replace")
                        ev = parse_log_line(text, time.time())
                        if ev:
                            try:
                                self.store.insert_event(self.instance, ev)
                            except Exception as e:
                                print(f"[tail {self.instance[:12]}] db: {e}",
                                      file=sys.stderr)
            except FileNotFoundError:
                print(f"[tail {self.instance[:12]}] '{self.sashi_bin}' not found",
                      file=sys.stderr)
                self.stop_event.wait(15)
            except Exception as e:
                print(f"[tail {self.instance[:12]}] {e}", file=sys.stderr)
            finally:
                self._cleanup()

            if not self.stop_event.is_set():
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    def _cleanup(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGINT)
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except Exception:
                pass
            self.master_fd = None

    def shutdown(self) -> None:
        self._cleanup()


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

class Discoverer(threading.Thread):
    def __init__(self, store: Store, tails: dict[str, Tail],
                 stop_event: threading.Event, sashi_bin: str,
                 interval: int = DISCOVER_INTERVAL):
        super().__init__(daemon=True, name="discover")
        self.store = store
        self.tails = tails
        self.stop_event = stop_event
        self.sashi_bin = sashi_bin
        self.interval = interval

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                out = subprocess.check_output(
                    [self.sashi_bin, "list"], text=True, timeout=15,
                )
                instances = json.loads(out)
                seen = set()
                for ins in instances:
                    name = ins.get("name")
                    if not name:
                        continue
                    seen.add(name)
                    self.store.upsert_instance(ins)
                    if name not in self.tails:
                        t = Tail(name, self.store, self.stop_event, self.sashi_bin)
                        self.tails[name] = t
                        t.start()
                        print(f"[discover] tailing {name[:16]}")
                # We intentionally don't reap stopped instances' tails;
                # they self-exit via the retry loop.
            except FileNotFoundError:
                print(f"[discover] '{self.sashi_bin}' not found in PATH",
                      file=sys.stderr)
            except Exception as e:
                print(f"[discover] {e}", file=sys.stderr)
            self.stop_event.wait(self.interval)


# --------------------------------------------------------------------------
# Retention sweeper
# --------------------------------------------------------------------------

class Pruner(threading.Thread):
    def __init__(self, store: Store, stop_event: threading.Event,
                 retention_days: int):
        super().__init__(daemon=True, name="pruner")
        self.store = store
        self.stop_event = stop_event
        self.retention_days = retention_days

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                n = self.store.prune(self.retention_days)
                if n:
                    print(f"[pruner] removed {n} old events")
            except Exception as e:
                print(f"[pruner] {e}", file=sys.stderr)
            self.stop_event.wait(RETENTION_SWEEP)


# --------------------------------------------------------------------------
# Embedded HTTP dashboard
# --------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sashimon</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root { color-scheme: dark; }
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif;
         margin: 0; background: #0d1117; color: #c9d1d9; }
  header { padding: 14px 20px; background: #161b22; border-bottom: 1px solid #30363d;
           display: flex; align-items: center; gap: 16px; }
  header h1 { margin: 0; font-size: 18px; }
  header .meta { font-size: 12px; color: #8b949e; }
  main { padding: 20px; max-width: 1400px; margin: 0 auto; }
  .controls { display: flex; gap: 12px; align-items: center; margin-bottom: 16px; }
  select, button { background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
                   padding: 6px 10px; border-radius: 6px; font-size: 13px; }
  button { cursor: pointer; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
          gap: 16px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
          padding: 14px; }
  .card h2 { margin: 0 0 4px; font-size: 14px; font-family: monospace; word-break: break-all; }
  .card .sub { font-size: 11px; color: #8b949e; margin-bottom: 10px; }
  .health { display: inline-block; padding: 2px 8px; border-radius: 12px;
            font-size: 11px; font-weight: 600; text-transform: uppercase; }
  .h-healthy        { background: #238636; color: #fff; }
  .h-consensus_loss { background: #b54708; color: #fff; }
  .h-forked         { background: #b62324; color: #fff; }
  .h-stalled        { background: #8957e5; color: #fff; }
  .h-unknown        { background: #484f58; color: #fff; }
  .stats { display: grid; grid-template-columns: 1fr 1fr; gap: 4px 12px;
           font-size: 12px; margin-bottom: 10px; }
  .stats span:nth-child(odd) { color: #8b949e; }
  canvas { width: 100% !important; height: 90px !important; }
  .global { margin-bottom: 22px; padding: 14px;
            background: #161b22; border: 1px solid #30363d; border-radius: 8px; }
  .global canvas { height: 220px !important; }
  .empty { text-align: center; color: #8b949e; padding: 60px 0; }
</style>
</head>
<body>
<header>
  <h1>sashimon</h1>
  <span class="meta">Sashimono HotPocket monitor</span>
  <span class="meta" id="lastUpdate"></span>
</header>
<main>
  <div class="controls">
    <label>Window
      <select id="window">
        <option value="900">15 min</option>
        <option value="3600" selected>1 hour</option>
        <option value="21600">6 hours</option>
        <option value="86400">24 hours</option>
      </select>
    </label>
    <label>Auto-refresh
      <select id="refresh">
        <option value="0">off</option>
        <option value="5000" selected>5s</option>
        <option value="15000">15s</option>
        <option value="60000">60s</option>
      </select>
    </label>
    <button id="reload">Reload</button>
  </div>
  <div class="global">
    <strong>All instances — events / minute</strong>
    <canvas id="globalChart"></canvas>
  </div>
  <div id="cards" class="grid"></div>
</main>
<script>
const TAG_COLORS = {
  ledger_created:   '#3fb950',
  contract_running: '#1f6feb',
  consensus_lost:   '#d29922',
  fork_warn:        '#f85149',
  out_of_sync:      '#bc8cff',
  error:            '#ff7b72',
  warning:          '#e3b341',
  hp_started:       '#79c0ff',
  hp_stopped:       '#8b949e',
  role_change:      '#56d4dd',
  info_other:       '#484f58',
};
const TAG_ORDER = [
  'ledger_created','contract_running','consensus_lost','fork_warn',
  'out_of_sync','error','warning','hp_started','hp_stopped',
  'role_change','info_other'
];

let charts = {};
let globalChart = null;
let timer = null;

function fmtAge(s) {
  if (s == null) return '—';
  if (s < 60) return s.toFixed(0) + 's';
  if (s < 3600) return (s/60).toFixed(1) + 'm';
  return (s/3600).toFixed(1) + 'h';
}

async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + ' ' + r.status);
  return r.json();
}

function buildDatasets(buckets, bucketSec) {
  const labels = buckets.map(b => new Date(b.bucket_start * 1000)
    .toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}));
  const datasets = TAG_ORDER
    .filter(tag => buckets.some(b => (b[tag] || 0) > 0))
    .map(tag => ({
      label: tag,
      data: buckets.map(b => b[tag] || 0),
      backgroundColor: TAG_COLORS[tag] || '#888',
      stack: 'a',
    }));
  return { labels, datasets };
}

function chartOpts(stacked = true) {
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    plugins: { legend: { display: stacked, labels: { color:'#c9d1d9', font:{size:10} } } },
    scales: {
      x: { stacked, ticks:{color:'#8b949e', font:{size:10}}, grid:{display:false} },
      y: { stacked, ticks:{color:'#8b949e', font:{size:10}}, grid:{color:'#21262d'} },
    },
  };
}

async function refresh() {
  const window = +document.getElementById('window').value;
  const bucketSec = window <= 900 ? 30 : window <= 3600 ? 60 : window <= 21600 ? 300 : 900;

  const [summary, globalBuckets] = await Promise.all([
    fetchJSON('/api/summary?window=' + window),
    fetchJSON(`/api/histogram?window=${window}&bucket=${bucketSec}`),
  ]);

  // Global chart
  const gData = buildDatasets(globalBuckets, bucketSec);
  if (!globalChart) {
    globalChart = new Chart(document.getElementById('globalChart'), {
      type: 'bar', data: gData, options: chartOpts(true),
    });
  } else {
    globalChart.data = gData;
    globalChart.update();
  }

  // Per-instance cards
  const cards = document.getElementById('cards');
  if (!summary.length) {
    cards.innerHTML = '<div class="empty">No Sashimono instances detected yet. ' +
      'Confirm <code>sashi list</code> works on this VM.</div>';
    return;
  }

  const seen = new Set();
  for (const inst of summary) {
    seen.add(inst.name);
    let card = document.getElementById('c-' + inst.name);
    if (!card) {
      card = document.createElement('div');
      card.id = 'c-' + inst.name;
      card.className = 'card';
      card.innerHTML = `
        <h2>${inst.name}</h2>
        <div class="sub" data-sub></div>
        <div class="stats" data-stats></div>
        <canvas id="ch-${inst.name}"></canvas>`;
      cards.appendChild(card);
    }
    card.querySelector('[data-sub]').innerHTML =
      `<span class="health h-${inst.health}">${inst.health}</span> · sashi: ${inst.sashi_status || '—'}`;
    const c = inst.counts || {};
    card.querySelector('[data-stats]').innerHTML = `
      <span>Last ledger</span><span>${fmtAge(inst.last_ledger_age_s)} ago</span>
      <span>Last event</span><span>${fmtAge(inst.last_event_age_s)} ago</span>
      <span>Ledgers (win)</span><span>${c.ledger_created || 0}</span>
      <span>Consensus loss</span><span>${c.consensus_lost || 0}</span>
      <span>Fork warnings</span><span>${c.fork_warn || 0}</span>
      <span>Errors</span><span>${c.error || 0}</span>
      <span>Uptime est.</span><span>${inst.uptime_pct}%</span>`;

    const buckets = await fetchJSON(
      `/api/histogram?window=${window}&bucket=${bucketSec}&instance=${encodeURIComponent(inst.name)}`);
    const data = buildDatasets(buckets, bucketSec);
    if (!charts[inst.name]) {
      charts[inst.name] = new Chart(document.getElementById('ch-' + inst.name), {
        type: 'bar', data, options: chartOpts(false),
      });
    } else {
      charts[inst.name].data = data;
      charts[inst.name].update();
    }
  }
  // Drop cards for vanished instances
  for (const k of Object.keys(charts)) {
    if (!seen.has(k)) {
      const el = document.getElementById('c-' + k);
      if (el) el.remove();
      charts[k].destroy();
      delete charts[k];
    }
  }
  document.getElementById('lastUpdate').textContent =
    'updated ' + new Date().toLocaleTimeString();
}

function scheduleRefresh() {
  if (timer) clearInterval(timer);
  const ms = +document.getElementById('refresh').value;
  if (ms > 0) timer = setInterval(refresh, ms);
}

document.getElementById('reload').onclick = refresh;
document.getElementById('window').onchange = () => {
  for (const k of Object.keys(charts)) { charts[k].destroy(); }
  charts = {};
  if (globalChart) { globalChart.destroy(); globalChart = null; }
  document.getElementById('cards').innerHTML = '';
  refresh();
};
document.getElementById('refresh').onchange = scheduleRefresh;

refresh().catch(e => {
  document.getElementById('cards').innerHTML =
    '<div class="empty">Error: ' + e.message + '</div>';
});
scheduleRefresh();
</script>
</body>
</html>
"""


def _parse_window(raw: str | None) -> int:
    """Returns seconds. 0 means 'all-time'."""
    if raw is None:
        return 3600
    s = raw.strip().lower()
    if s in ("all", "0", ""):
        return 0
    try:
        return max(0, int(s))
    except ValueError:
        return 3600


def make_handler(store: Store, static_html_path: str | None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:  # silence default logger
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _serve_dashboard(self) -> None:
            html: bytes
            if static_html_path:
                try:
                    with open(static_html_path, "rb") as f:
                        html = f.read()
                except OSError:
                    html = DASHBOARD_HTML.encode("utf-8")
            else:
                html = DASHBOARD_HTML.encode("utf-8")
            self._send(200, html, "text/html; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            qs = parse_qs(u.query)

            if u.path in ("/", "/index.html"):
                self._serve_dashboard()
                return

            if u.path == "/api/instances":
                self._json(store.list_instances())
                return

            if u.path == "/api/summary":
                w = _parse_window(qs.get("window", [None])[0])
                self._json(store.summary(w))
                return

            if u.path == "/api/events":
                inst = qs.get("instance", [None])[0]
                since = float(qs["since"][0]) if "since" in qs else None
                until = float(qs["until"][0]) if "until" in qs else None
                tag = qs.get("tag", [None])[0]
                limit = int(qs.get("limit", ["2000"])[0])
                self._json(store.events_window(inst, since, until, tag, limit))
                return

            if u.path == "/api/histogram":
                inst = qs.get("instance", [None])[0]
                window = _parse_window(qs.get("window", [None])[0])
                bucket = int(qs.get("bucket", ["60"])[0])
                until = time.time()
                if window <= 0:
                    earliest = store.earliest_event_ts(inst)
                    since = earliest if earliest is not None else until - 60
                else:
                    since = until - window
                self._json(store.histogram(inst, since, until, bucket))
                return

            if u.path == "/api/spells":
                inst = qs.get("instance", [None])[0]
                window = _parse_window(qs.get("window", [None])[0])
                until = time.time()
                if window <= 0:
                    earliest = store.earliest_event_ts(inst)
                    since = earliest if earliest is not None else until - 60
                else:
                    since = until - window
                tags_raw = qs.get("tags", [None])[0]
                tags: tuple[str, ...]
                if tags_raw:
                    tags = tuple(t for t in tags_raw.split(",") if t)
                else:
                    tags = ("consensus_lost", "fork_warn", "out_of_sync",
                            "error", "warning")
                try:
                    max_gap = float(qs.get("max_gap", ["10"])[0])
                except ValueError:
                    max_gap = 10.0
                try:
                    min_count = int(qs.get("min_count", ["1"])[0])
                except ValueError:
                    min_count = 1
                self._json(store.spells(
                    instance=inst, since=since, until=until,
                    tags=tags, max_gap=max_gap, min_count=min_count,
                ))
                return

            if u.path == "/healthz":
                self._json({"ok": True})
                return

            self._send(404, b"not found", "text/plain")

    return Handler


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Sashimono HotPocket monitor")
    ap.add_argument("--db",    default=os.environ.get("SASHIMON_DB", DEFAULT_DB))
    ap.add_argument("--port",  type=int,
                    default=int(os.environ.get("SASHIMON_PORT", DEFAULT_PORT)))
    ap.add_argument("--bind",  default=os.environ.get("SASHIMON_BIND", DEFAULT_BIND))
    ap.add_argument("--sashi", default=os.environ.get("SASHIMON_SASHI", "sashi"),
                    help="sashi binary (default: sashi from PATH)")
    ap.add_argument("--retention-days", type=int,
                    default=int(os.environ.get("SASHIMON_RETENTION_DAYS",
                                               RETENTION_DAYS)))
    ap.add_argument("--static",
                    default=os.environ.get("SASHIMON_STATIC", ""),
                    help="Path to dashboard index.html. Defaults to "
                         "<script-dir>/index.html if it exists.")
    args = ap.parse_args()

    sashi_path = shutil.which(args.sashi) or args.sashi
    store = Store(args.db)
    stop = threading.Event()
    tails: dict[str, Tail] = {}

    static_path = args.static.strip() or str(
        Path(__file__).resolve().parent / "index.html"
    )
    if not Path(static_path).exists():
        static_path = ""

    def _term(_sig, _frm):
        print("[sashimon] shutting down")
        stop.set()
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)

    Discoverer(store, tails, stop, sashi_path).start()
    Pruner(store, stop, args.retention_days).start()

    server = ThreadingHTTPServer(
        (args.bind, args.port),
        make_handler(store, static_path or None),
    )
    print(f"[sashimon] db={args.db}  sashi={sashi_path}")
    print(f"[sashimon] static={static_path or '(embedded fallback)'}")
    print(f"[sashimon] dashboard http://{args.bind}:{args.port}")
    threading.Thread(target=server.serve_forever, daemon=True,
                     name="http").start()
    try:
        while not stop.is_set():
            time.sleep(1.0)
    finally:
        server.shutdown()
        for t in list(tails.values()):
            t.shutdown()


if __name__ == "__main__":
    main()
