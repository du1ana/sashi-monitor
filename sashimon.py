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

# Make all PEP-604 (`X | Y`) annotations lazy strings so this file imports
# cleanly on Python 3.8 / 3.9 (e.g. Ubuntu 20.04, which is the default
# Sashimono host OS). Runtime type checks here use isinstance/get(), never
# the annotation objects themselves, so PEP 563 is safe.
from __future__ import annotations

import argparse
import json
import os
import pty
import re
import select
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
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

# Host-metrics / error-spell capture (§8 of HOTPOCKET_CONSENSUS_INVESTIGATION.md)
METRICS_INTERVAL = 30         # seconds between host-metric samples (normal)
METRICS_INTERVAL_BOOST = 3    # seconds between samples while a spell is active
METRICS_BOOST_COOLDOWN = 120  # keep boosted sampling this long after last error tag
SNAPSHOT_RECAPTURE = 60       # if still in-spell, recapture ps/df/journalctl after this many s
SNAPSHOT_MAX_CAPTURES = 3     # cap captures per spell
HARD_FORK_AFTER = 120         # an open spell older than this (no ledger since) ≈ hard fork
DEFAULT_ROUNDTIME_MS = 2000   # HotPocket roundtime; used for the uptime-% denominator
# Tags that mean "the cluster/this node is in trouble".
ERROR_TAGS = ("fork_warn", "consensus_lost", "out_of_sync", "error")
# Mounts always sampled in addition to whatever holds the contract dirs.
ALWAYS_SAMPLE_MOUNTS = ("/", "/var/lib")

# --------------------------------------------------------------------------
# Severity + tracking policy
# --------------------------------------------------------------------------
# Treat fork conditions as high-impact (boost metrics + snapshot + always
# track events). Consensus-loss / out-of-sync / plain warnings are low-impact
# noise that bloats the DB during a spell — keep the spell row, but drop
# the per-event flood and skip metric boost.
TAG_SEVERITY = {
    "fork_warn":      "high",
    "error":          "high",
    "out_of_sync":    "low",
    "consensus_lost": "low",
    "warning":        "low",
}

# Default settings — overridable at runtime via /api/policy (persisted in the
# `settings` table). `policy_mode` picks one of the presets below.
DEFAULT_POLICY_MODE = "balanced"

POLICY_MODES = {
    # Legacy behaviour: track every event, boost metrics + snapshot for every
    # spell regardless of severity.
    "full": {
        "low":  {"events": True,  "boost": True,  "snapshot": True},
        "high": {"events": True,  "boost": True,  "snapshot": True},
    },
    # Default — only fork-class spells get the heavy treatment. Low-severity
    # spells (consensus_lost, out_of_sync, warning) skip metric boost +
    # snapshots, and during the spell their event-flood is dropped.
    "balanced": {
        "low":  {"events": False, "boost": False, "snapshot": False},
        "high": {"events": True,  "boost": True,  "snapshot": True},
    },
    # Most aggressive — only track ledger_created + high-severity events;
    # never boost metrics or snapshot.
    "minimal": {
        "low":  {"events": False, "boost": False, "snapshot": False},
        "high": {"events": True,  "boost": False, "snapshot": False},
    },
}

# Tags that are ALWAYS persisted regardless of policy gating (so we can still
# tell healthy from forked, and so a spell can open from its trigger event).
ALWAYS_TRACK_TAGS = {"ledger_created", "fork_warn", "error",
                     "hp_started", "hp_stopped"}


def tag_severity(tag: str) -> str:
    return TAG_SEVERITY.get(tag, "high" if tag in ERROR_TAGS else "low")

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
# Host metrics & shell helpers
# --------------------------------------------------------------------------
#
# sashimon runs as root on the VM that hosts the HotPocket instances, so we
# read /proc and statvfs directly. Everything here is best-effort: a missing
# file or an unavailable tool yields None for that field, never an exception.

def run_cmd(cmd: list[str], timeout: float = 8.0, want_err: bool = False) -> str | None:
    """Run a command, return its stdout (str), or None on any failure/timeout."""
    try:
        cp = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
        out = cp.stdout or ""
        if want_err and cp.stderr:
            out = (out + "\n" + cp.stderr).strip()
        return out
    except Exception:
        return None


def _read_text(path: str) -> str | None:
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except Exception:
        return None


def _cpu_jiffies() -> tuple[int, int, int] | None:
    """(idle+iowait, total, steal) jiffies from /proc/stat."""
    txt = _read_text("/proc/stat")
    if not txt:
        return None
    for line in txt.splitlines():
        if line.startswith("cpu "):
            v = [int(x) for x in line.split()[1:]]
            idle = v[3] + (v[4] if len(v) > 4 else 0)
            steal = v[7] if len(v) > 7 else 0
            return idle, sum(v), steal
    return None


def _loadavg() -> tuple[float, float, float] | None:
    txt = _read_text("/proc/loadavg")
    if not txt:
        return None
    try:
        p = txt.split()
        return float(p[0]), float(p[1]), float(p[2])
    except Exception:
        return None


def _meminfo() -> dict[str, int] | None:
    txt = _read_text("/proc/meminfo")
    if not txt:
        return None
    d: dict[str, int] = {}
    for line in txt.splitlines():
        k, _, rest = line.partition(":")
        try:
            d[k.strip()] = int(rest.strip().split()[0])  # kB
        except Exception:
            pass
    return d or None


def _net_totals() -> tuple[int, int] | None:
    """(rx_bytes, tx_bytes) summed over non-loopback interfaces."""
    txt = _read_text("/proc/net/dev")
    if not txt:
        return None
    rx = tx = 0
    for line in txt.splitlines()[2:]:
        iface, _, data = line.partition(":")
        iface = iface.strip()
        if not data or iface in ("lo",):
            continue
        c = data.split()
        try:
            rx += int(c[0]); tx += int(c[8])
        except Exception:
            pass
    return rx, tx


def _sys_open_fds() -> int | None:
    txt = _read_text("/proc/sys/fs/file-nr")
    if not txt:
        return None
    try:
        return int(txt.split()[0])
    except Exception:
        return None


def _disk_usage(path: str) -> dict | None:
    try:
        st = os.statvfs(path)
    except Exception:
        return None
    blocks = st.f_blocks or 1
    files = st.f_files or 1
    return {
        "free_mb": st.f_bavail * st.f_frsize / 1048576.0,
        "used_pct": 100.0 * (1.0 - st.f_bavail / blocks),
        "inode_used_pct": 100.0 * (1.0 - st.f_favail / files),
    }


def _ntp_status() -> tuple[float | None, int | None]:
    """(last_offset_ms, synced 0/1). Tries chronyc, then timedatectl."""
    # chronyc -c tracking : csv; field 5 (0-based) = 'last offset' in seconds.
    out = run_cmd(["chronyc", "-c", "tracking"], timeout=4)
    if out:
        try:
            f = out.strip().split(",")
            return float(f[5]) * 1000.0, 1
        except Exception:
            pass
    out = run_cmd(["chronyc", "tracking"], timeout=4)
    if out:
        m = re.search(r"Last offset\s*:\s*([-+0-9.eE]+)\s*seconds", out)
        leap = re.search(r"Leap status\s*:\s*(\w+)", out)
        if m:
            synced = 1 if (leap and leap.group(1).lower() == "normal") else 1
            return float(m.group(1)) * 1000.0, synced
    out = run_cmd(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], timeout=4)
    if out is not None:
        return None, (1 if out.strip().lower() in ("yes", "true", "1") else 0)
    return None, None


def _find_instance_pid(name: str) -> int | None:
    """Best-effort: the pid of the HotPocket process for an instance.

    Sashimono runs each instance under its own Linux user and/or container; the
    instance name (or contract id) usually appears in the process cmdline. We
    scan /proc rather than relying on pgrep so it works without extra tooling.
    """
    if not name:
        return None
    needle = name.encode()
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as f:
                    cl = f.read()
            except Exception:
                continue
            if needle in cl and (b"hpcore" in cl or b"hp.core" in cl or b"node" in cl or b"sashi" in cl):
                return int(entry)
    except Exception:
        return None
    # Fallback: pgrep -f.
    out = run_cmd(["pgrep", "-f", name], timeout=3)
    if out:
        try:
            return int(out.split()[0])
        except Exception:
            return None
    return None


def _proc_rss_mb(pid: int) -> float | None:
    txt = _read_text(f"/proc/{pid}/status")
    if not txt:
        return None
    m = re.search(r"^VmRSS:\s*(\d+)\s*kB", txt, re.M)
    return (int(m.group(1)) / 1024.0) if m else None


def _proc_open_fds(pid: int) -> int | None:
    try:
        return len(os.listdir(f"/proc/{pid}/fd"))
    except Exception:
        return None


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

-- One row per host-metric sample. `instance` is NULL for machine-wide samples
-- and set for per-HP-process samples taken at the same `ts`. `during_spell`
-- and `spell_id` mark samples taken while an error spell was active (and at the
-- boosted rate). `extra` holds JSON for fields without their own column
-- (per-mount disk, etc.).
CREATE TABLE IF NOT EXISTS host_metrics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    instance      TEXT,
    during_spell  INTEGER DEFAULT 0,
    spell_id      TEXT,
    cpu_pct       REAL,
    steal_pct     REAL,
    load1         REAL,
    load5         REAL,
    mem_used_pct  REAL,
    mem_avail_mb  REAL,
    swap_used_mb  REAL,
    disk_used_pct REAL,   -- worst (max) used% across sampled mounts
    disk_free_mb  REAL,   -- min free across sampled mounts
    inode_used_pct REAL,  -- worst across sampled mounts
    net_rx_kbps   REAL,
    net_tx_kbps   REAL,
    sys_open_fds  INTEGER,
    proc_rss_mb   REAL,   -- per-instance rows only
    proc_open_fds INTEGER,-- per-instance rows only
    proc_pid      INTEGER,-- per-instance rows only
    ntp_offset_ms REAL,
    ntp_synced    INTEGER,
    extra         TEXT
);
CREATE INDEX IF NOT EXISTS idx_hm_ts        ON host_metrics(ts);
CREATE INDEX IF NOT EXISTS idx_hm_inst_ts   ON host_metrics(instance, ts);
CREATE INDEX IF NOT EXISTS idx_hm_spell     ON host_metrics(spell_id);

-- Lifecycle of error spells: one row per spell, opened when an instance first
-- emits an error tag while not already in a spell, closed when it next emits
-- ledger_created (recovered=1). An open row (end_ts NULL) older than
-- HARD_FORK_AFTER ≈ a hard fork that never recovered.
CREATE TABLE IF NOT EXISTS spells_log (
    spell_id     TEXT PRIMARY KEY,
    instance     TEXT NOT NULL,
    start_ts     REAL NOT NULL,
    end_ts       REAL,
    recovered    INTEGER,        -- 1 if a ledger_created closed it; NULL while open
    trigger_tag  TEXT,
    trigger_msg  TEXT,
    captures     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_spells_start ON spells_log(start_ts);
CREATE INDEX IF NOT EXISTS idx_spells_inst  ON spells_log(instance, start_ts);

-- Free-form diagnostic snapshots captured when a spell starts (and re-captured
-- while it persists): `ps`, `df`, `journalctl`, `dmesg`, `chronyc`, tail of the
-- HP log, contract dir sizes, etc.
CREATE TABLE IF NOT EXISTS spell_artifacts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    spell_id  TEXT NOT NULL,
    ts        REAL NOT NULL,
    instance  TEXT,            -- NULL for machine-wide artifacts
    kind      TEXT NOT NULL,   -- 'ps' | 'df' | 'dfi' | 'journalctl' | 'dmesg' | 'chronyc' | 'free' | 'vmstat' | 'uptime' | 'du' | 'logtail'
    content   TEXT
);
CREATE INDEX IF NOT EXISTS idx_artifacts_spell ON spell_artifacts(spell_id, ts);

-- Runtime settings (tracking policy, etc.). Single-row key/value store so the
-- frontend can flip the DB-tracking mode without restarting the daemon.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Discovered clusters (grouping of instances by Sashimono contract_id). The
-- daemon only tails instances whose cluster is marked monitored=1; everything
-- else is still listed (so the operator can opt in from the dashboard) but
-- not consuming pty resources.
CREATE TABLE IF NOT EXISTS clusters (
    contract_id  TEXT PRIMARY KEY,
    label        TEXT,
    monitored    INTEGER NOT NULL DEFAULT 0,
    first_seen   REAL,
    last_seen    REAL
);
"""

# Sentinel contract id for instances missing a real contract_id field.
NO_CONTRACT_ID = "_unknown"


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
        contract_id: str | None = None,
    ) -> list[dict]:
        q = ("SELECT instance, ts, level, module, tag, msg "
             "FROM events WHERE 1=1")
        args: list = []
        if instance:
            q += " AND instance=?"; args.append(instance)
        if contract_id:
            q += " AND instance IN (SELECT name FROM instances WHERE contract_id=?)"
            args.append(contract_id)
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
        contract_id: str | None = None,
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
        if contract_id:
            q += "AND instance IN (SELECT name FROM instances WHERE contract_id=?) "
            args.append(contract_id)
        q += "GROUP BY bkt, tag ORDER BY bkt"
        with self.lock:
            rows = self.conn.execute(q, args).fetchall()
        out: dict[int, dict] = {}
        for bkt, tag, cnt in rows:
            slot = out.setdefault(int(bkt), {"bucket_start": since + int(bkt) * bucket_seconds})
            slot[tag] = cnt
        return [out[k] for k in sorted(out)]

    def summary(self, window_seconds: int = 3600,
                roundtime_ms: int = DEFAULT_ROUNDTIME_MS,
                contract_id: str | None = None) -> list[dict]:
        """window_seconds=0 means all-time. roundtime_ms drives the uptime-%
        denominator (one ledger expected per roundtime). Optional contract_id
        scopes the summary to a single cluster."""
        now = time.time()
        round_s = max(0.5, roundtime_ms / 1000.0)
        all_time = (window_seconds <= 0)
        since = 0.0 if all_time else now - window_seconds
        results: list[dict] = []
        with self.lock:
            if contract_id:
                inst_rows = self.conn.execute(
                    "SELECT name, status FROM instances "
                    "WHERE contract_id=? ORDER BY name", (contract_id,)
                ).fetchall()
            else:
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
                last_fork = self.conn.execute(
                    "SELECT MAX(ts) FROM events "
                    "WHERE instance=? AND tag='fork_warn'",
                    (name,),
                ).fetchone()[0]
                last_cons_lost = self.conn.execute(
                    "SELECT MAX(ts) FROM events "
                    "WHERE instance=? AND tag='consensus_lost'",
                    (name,),
                ).fetchone()[0]
                last_oos = self.conn.execute(
                    "SELECT MAX(ts) FROM events "
                    "WHERE instance=? AND tag='out_of_sync'",
                    (name,),
                ).fetchone()[0]

                ledger_age = (now - last_ledger) if last_ledger else None
                event_age = (now - last_event) if last_event else None

                # Health is derived from current state, not aggregate counts.
                # A recent ledger trumps any past error: if we're producing
                # ledgers within STALL_THRESHOLD, we're healthy.
                fresh_ledger = (
                    last_ledger is not None
                    and (now - last_ledger) <= STALL_THRESHOLD
                )
                # Most-recent error tag wins if no fresh ledger.
                err_candidates = [
                    ("forked",          last_fork),
                    ("consensus_loss",  last_cons_lost),
                    ("consensus_loss",  last_oos),
                ]
                err_candidates = [(h, ts) for h, ts in err_candidates if ts]
                latest_err = max(err_candidates, key=lambda x: x[1]) if err_candidates else None

                if fresh_ledger and (
                    latest_err is None or latest_err[1] < last_ledger
                ):
                    health = "healthy"
                elif latest_err is not None:
                    health = latest_err[0]
                elif event_age is not None and event_age > STALL_THRESHOLD:
                    health = "stalled"
                elif fresh_ledger:
                    health = "healthy"
                else:
                    health = "unknown"

                # Approx uptime % = fraction of roundtime slots producing a
                # ledger. For all-time, use the span between first-seen and now.
                if all_time:
                    first_ts = self.conn.execute(
                        "SELECT MIN(ts) FROM events WHERE instance=?", (name,)
                    ).fetchone()[0]
                    span = max(round_s, (now - first_ts)) if first_ts else round_s
                else:
                    span = max(round_s, float(window_seconds))
                slots = max(1, int(span // round_s))
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

    # ---- host metrics --------------------------------------------------

    _HM_COLS = (
        "ts", "instance", "during_spell", "spell_id", "cpu_pct", "steal_pct",
        "load1", "load5", "mem_used_pct", "mem_avail_mb", "swap_used_mb",
        "disk_used_pct", "disk_free_mb", "inode_used_pct", "net_rx_kbps",
        "net_tx_kbps", "sys_open_fds", "proc_rss_mb", "proc_open_fds",
        "proc_pid", "ntp_offset_ms", "ntp_synced", "extra",
    )

    def insert_host_metrics(self, rows: list[dict]) -> None:
        if not rows:
            return
        ph = ",".join(["?"] * len(self._HM_COLS))
        with self.lock:
            self.conn.executemany(
                f"INSERT INTO host_metrics ({','.join(self._HM_COLS)}) VALUES ({ph})",
                [tuple(r.get(c) for c in self._HM_COLS) for r in rows],
            )
            self.conn.commit()

    def host_metrics_window(self, instance: str | None, since: float,
                            until: float | None = None, limit: int = 20000) -> list[dict]:
        if until is None:
            until = time.time()
        q = "SELECT * FROM host_metrics WHERE ts>=? AND ts<=?"
        args: list = [since, until]
        # instance="" / "machine" => machine-wide rows (instance IS NULL)
        if instance in ("", "machine", "_host"):
            q += " AND instance IS NULL"
        elif instance:
            q += " AND instance=?"; args.append(instance)
        q += " ORDER BY ts DESC LIMIT ?"; args.append(limit)
        with self.lock:
            cur = self.conn.execute(q, args)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def latest_host_metric(self) -> dict | None:
        with self.lock:
            cur = self.conn.execute(
                "SELECT * FROM host_metrics WHERE instance IS NULL ORDER BY ts DESC LIMIT 1")
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            return dict(zip(cols, row)) if row else None

    # ---- spells lifecycle ---------------------------------------------

    def open_spell(self, spell_id: str, instance: str, start_ts: float,
                   trigger_tag: str, trigger_msg: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO spells_log "
                "(spell_id, instance, start_ts, trigger_tag, trigger_msg) "
                "VALUES (?,?,?,?,?)",
                (spell_id, instance, start_ts, trigger_tag, (trigger_msg or "")[:500]),
            )
            self.conn.commit()

    def close_spell(self, spell_id: str, end_ts: float, recovered: int) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE spells_log SET end_ts=?, recovered=? "
                "WHERE spell_id=? AND end_ts IS NULL",
                (end_ts, recovered, spell_id),
            )
            self.conn.commit()

    def spell_inc_captures(self, spell_id: str) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE spells_log SET captures = COALESCE(captures,0)+1 WHERE spell_id=?",
                (spell_id,))
            self.conn.commit()

    def open_spells(self) -> list[dict]:
        with self.lock:
            cur = self.conn.execute(
                "SELECT * FROM spells_log WHERE end_ts IS NULL ORDER BY start_ts")
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def spells_log_window(self, since: float, until: float | None = None,
                          instance: str | None = None, limit: int = 500,
                          contract_id: str | None = None) -> list[dict]:
        if until is None:
            until = time.time()
        q = "SELECT * FROM spells_log WHERE start_ts>=? AND start_ts<=?"
        args: list = [since, until]
        if instance:
            q += " AND instance=?"; args.append(instance)
        if contract_id:
            q += " AND instance IN (SELECT name FROM instances WHERE contract_id=?)"
            args.append(contract_id)
        q += " ORDER BY start_ts DESC LIMIT ?"; args.append(limit)
        now = time.time()
        with self.lock:
            cur = self.conn.execute(q, args)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            if r["end_ts"] is None:
                age = now - r["start_ts"]
                r["state"] = "hard_fork?" if age > HARD_FORK_AFTER else "active"
                r["duration_s"] = age
            else:
                r["state"] = "recovered" if r.get("recovered") else "ended"
                r["duration_s"] = r["end_ts"] - r["start_ts"]
        return rows

    def add_spell_artifact(self, spell_id: str, instance: str | None,
                           kind: str, content: str | None, ts: float | None = None) -> None:
        if content is None:
            return
        with self.lock:
            self.conn.execute(
                "INSERT INTO spell_artifacts (spell_id, ts, instance, kind, content) "
                "VALUES (?,?,?,?,?)",
                (spell_id, ts or time.time(), instance, kind, content[:200000]),
            )
            self.conn.commit()

    def spell_artifacts(self, spell_id: str) -> list[dict]:
        with self.lock:
            cur = self.conn.execute(
                "SELECT id, ts, instance, kind, content FROM spell_artifacts "
                "WHERE spell_id=? ORDER BY ts, id", (spell_id,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def recent_log_lines(self, instance: str, limit: int = 200) -> list[dict]:
        with self.lock:
            cur = self.conn.execute(
                "SELECT ts, level, module, tag, msg FROM events "
                "WHERE instance=? ORDER BY ts DESC LIMIT ?", (instance, limit))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        rows.reverse()
        return rows

    def prune(self, retention_days: int) -> int:
        cutoff = time.time() - retention_days * 86400
        with self.lock:
            cur = self.conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self.conn.execute("DELETE FROM host_metrics WHERE ts < ?", (cutoff,))
            self.conn.execute("DELETE FROM spell_artifacts WHERE ts < ?", (cutoff,))
            self.conn.execute(
                "DELETE FROM spells_log WHERE COALESCE(end_ts, start_ts) < ?", (cutoff,))
            self.conn.commit()
            return cur.rowcount

    # ---- clusters -----------------------------------------------------

    def upsert_cluster(self, contract_id: str, label: str | None = None) -> None:
        """Record a cluster the daemon has seen. `monitored` defaults to 0
        for any newly-discovered cluster; the operator opts in from the UI."""
        now = time.time()
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO clusters (contract_id, label, monitored, first_seen, last_seen)
                VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(contract_id) DO UPDATE SET
                  label     = COALESCE(excluded.label, clusters.label),
                  last_seen = excluded.last_seen
                """,
                (contract_id, label, now, now),
            )
            self.conn.commit()

    def set_cluster_monitored(self, contract_id: str, monitored: bool) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE clusters SET monitored=? WHERE contract_id=?",
                (1 if monitored else 0, contract_id),
            )
            self.conn.commit()

    def cluster_monitored_set(self) -> set[str]:
        with self.lock:
            return {r[0] for r in self.conn.execute(
                "SELECT contract_id FROM clusters WHERE monitored=1"
            )}

    def list_clusters(self) -> list[dict]:
        """Return clusters with rolled-up instance metadata. Cheaper than a
        join via the dashboard because this runs once on demand."""
        with self.lock:
            cluster_rows = self.conn.execute(
                "SELECT contract_id, label, monitored, first_seen, last_seen "
                "FROM clusters ORDER BY monitored DESC, last_seen DESC"
            ).fetchall()
            inst_rows = self.conn.execute(
                "SELECT name, contract_id, tenant, image, status, last_seen "
                "FROM instances ORDER BY name"
            ).fetchall()
        by_cid: dict[str, list[dict]] = {}
        for name, cid, tenant, image, status, last_seen in inst_rows:
            by_cid.setdefault(cid or NO_CONTRACT_ID, []).append({
                "name": name, "tenant": tenant, "image": image,
                "status": status, "last_seen": last_seen,
            })
        out = []
        seen_cids = set()
        for cid, label, monitored, first_seen, last_seen in cluster_rows:
            seen_cids.add(cid)
            insts = by_cid.get(cid, [])
            tenants = sorted({i["tenant"] for i in insts if i.get("tenant")})
            images  = sorted({i["image"]  for i in insts if i.get("image")})
            out.append({
                "contract_id": cid,
                "label":       label,
                "monitored":   bool(monitored),
                "first_seen":  first_seen,
                "last_seen":   last_seen,
                "node_count":  len(insts),
                "tenants":     tenants,
                "images":      images,
                "instances":   insts,
            })
        # Surface instances whose contract_id has no row in `clusters` yet
        # (race condition between upserts) so the UI never hides them.
        for cid, insts in by_cid.items():
            if cid in seen_cids:
                continue
            out.append({
                "contract_id": cid,
                "label":       None,
                "monitored":   False,
                "first_seen":  None,
                "last_seen":   max((i.get("last_seen") or 0) for i in insts) if insts else None,
                "node_count":  len(insts),
                "tenants":     sorted({i["tenant"] for i in insts if i.get("tenant")}),
                "images":      sorted({i["image"]  for i in insts if i.get("image")}),
                "instances":   insts,
            })
        return out

    def instance_names_for_cluster(self, contract_id: str) -> list[str]:
        with self.lock:
            return [r[0] for r in self.conn.execute(
                "SELECT name FROM instances WHERE contract_id=?", (contract_id,)
            )]

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self.conn.commit()

    def db_size_bytes(self) -> int:
        """Sum of the SQLite main file + its -wal and -shm sidecars (best-effort)."""
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return total

    def clear_all(self) -> dict:
        """Wipe all events and instance rows. Tail threads keep running;
        the next discovery poll will re-upsert live instances."""
        with self.lock:
            ev = self.conn.execute("DELETE FROM events").rowcount
            ins = self.conn.execute("DELETE FROM instances").rowcount
            hm = self.conn.execute("DELETE FROM host_metrics").rowcount
            sp = self.conn.execute("DELETE FROM spells_log").rowcount
            ar = self.conn.execute("DELETE FROM spell_artifacts").rowcount
            self.conn.commit()
            try:
                self.conn.execute("VACUUM")
            except sqlite3.OperationalError:
                pass
            return {"events_deleted": ev, "instances_deleted": ins,
                    "host_metrics_deleted": hm, "spells_deleted": sp,
                    "artifacts_deleted": ar}


# --------------------------------------------------------------------------
# Tail worker (one per instance)
# --------------------------------------------------------------------------

class PolicyManager:
    """Resolves the current event-tracking + spell-handling policy.

    Backed by the `settings` table so the dashboard can flip the mode at
    runtime; reads are cheap (hits SQLite per call but only on the ingest
    path, which is already DB-bound).
    """

    def __init__(self, store: Store, default_mode: str = DEFAULT_POLICY_MODE):
        self.store = store
        self.default = default_mode if default_mode in POLICY_MODES else "balanced"
        if self.store.get_setting("policy_mode") is None:
            self.store.set_setting("policy_mode", self.default)

    def mode(self) -> str:
        m = self.store.get_setting("policy_mode", self.default) or self.default
        return m if m in POLICY_MODES else self.default

    def set_mode(self, mode: str) -> str:
        if mode not in POLICY_MODES:
            raise ValueError(f"unknown policy mode: {mode}")
        self.store.set_setting("policy_mode", mode)
        return mode

    def spell_actions(self, severity: str) -> dict:
        """Return {events, boost, snapshot} bools for a spell of given severity
        under the current mode."""
        return POLICY_MODES[self.mode()].get(
            severity, POLICY_MODES[self.mode()]["high"]
        )

    def should_track_event(self, spell_mgr, instance: str, tag: str) -> bool:
        """Apply the per-event gate. ALWAYS_TRACK_TAGS bypass everything."""
        if tag in ALWAYS_TRACK_TAGS:
            return True
        mode = self.mode()
        if mode == "full":
            return True
        sev = tag_severity(tag)
        if mode == "minimal":
            return sev == "high"
        # balanced: only gate while the instance is in a low-severity spell.
        spell = spell_mgr.current_spell(instance) if spell_mgr else None
        if spell is None:
            return True
        return POLICY_MODES["balanced"][spell["severity"]]["events"]


class Tail(threading.Thread):
    def __init__(self, instance: str, store: Store, stop_event: threading.Event,
                 sashi_bin: str, spell_manager=None, policy: "PolicyManager | None" = None,
                 contract_id: str | None = None):
        super().__init__(daemon=True, name=f"tail-{instance[:12]}")
        self.instance = instance
        self.contract_id = contract_id
        self.store = store
        # `stop_event` is the global daemon stop; `local_stop` is set when this
        # tail alone should die (cluster un-monitored at runtime). Either one
        # exits the loop.
        self.stop_event = stop_event
        self.local_stop = threading.Event()
        self.sashi_bin = sashi_bin
        self.spell_manager = spell_manager
        self.policy = policy
        self.proc: subprocess.Popen | None = None
        self.master_fd: int | None = None

    def _should_stop(self) -> bool:
        return self.stop_event.is_set() or self.local_stop.is_set()

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
        while not self._should_stop():
            try:
                self._start_proc()
                buf = b""
                backoff = 1.0
                while not self._should_stop():
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
                            # Spell tracking always runs (so spells open/close
                            # regardless of event-storage policy).
                            if self.spell_manager is not None:
                                try:
                                    self.spell_manager.on_event(self.instance, ev)
                                except Exception as e:
                                    print(f"[tail {self.instance[:12]}] spell: {e}",
                                          file=sys.stderr)
                            track = True
                            if self.policy is not None:
                                try:
                                    track = self.policy.should_track_event(
                                        self.spell_manager, self.instance, ev["tag"])
                                except Exception:
                                    track = True
                            if track:
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

            if not self._should_stop():
                # Honour whichever signal trips first.
                self.stop_event.wait(backoff)
                if self.local_stop.is_set():
                    break
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
        self.local_stop.set()
        self._cleanup()


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

class Discoverer(threading.Thread):
    def __init__(self, store: Store, tails: dict[str, Tail],
                 stop_event: threading.Event, sashi_bin: str,
                 interval: int = DISCOVER_INTERVAL, spell_manager=None,
                 policy: "PolicyManager | None" = None,
                 auto_monitor_new: bool = False):
        super().__init__(daemon=True, name="discover")
        self.store = store
        self.tails = tails
        self.stop_event = stop_event
        self.sashi_bin = sashi_bin
        self.interval = interval
        self.spell_manager = spell_manager
        self.policy = policy
        self.auto_monitor_new = auto_monitor_new
        self._wake = threading.Event()

    def trigger(self) -> None:
        """Force the next discovery pass immediately. Called by /api/discover_now
        and when the user toggles a cluster's monitored state from the UI."""
        self._wake.set()

    def discover_once(self) -> dict:
        """Run one discovery pass; return {clusters_seen, instances_seen,
        tails_started, tails_reaped}."""
        try:
            out = subprocess.check_output(
                [self.sashi_bin, "list"], text=True, timeout=15,
            )
        except FileNotFoundError:
            print(f"[discover] '{self.sashi_bin}' not found in PATH",
                  file=sys.stderr)
            return {"error": "sashi not found"}
        except Exception as e:
            print(f"[discover] {e}", file=sys.stderr)
            return {"error": str(e)}

        try:
            instances = json.loads(out)
        except Exception as e:
            print(f"[discover] bad json: {e}", file=sys.stderr)
            return {"error": f"bad json: {e}"}

        seen_clusters: set[str] = set()
        seen_instances: set[str] = set()
        # First pass: persist everything we saw, so the UI can show all
        # clusters even before they're monitored.
        for ins in instances:
            name = ins.get("name")
            if not name:
                continue
            cid = ins.get("contract_id") or NO_CONTRACT_ID
            seen_clusters.add(cid)
            seen_instances.add(name)
            self.store.upsert_instance(ins)
            self.store.upsert_cluster(cid)

        # Optionally flip newly-seen clusters to monitored=1 (initial-install
        # convenience; off by default).
        if self.auto_monitor_new and seen_clusters:
            currently_monitored = self.store.cluster_monitored_set()
            for cid in seen_clusters:
                if cid not in currently_monitored:
                    self.store.set_cluster_monitored(cid, True)

        # Map instance->contract_id so we know which tails to keep.
        inst_to_cid: dict[str, str] = {}
        for ins in instances:
            n = ins.get("name")
            if n:
                inst_to_cid[n] = ins.get("contract_id") or NO_CONTRACT_ID

        monitored = self.store.cluster_monitored_set()
        started = 0
        reaped = 0

        # Start tails for monitored, not-yet-tailed instances.
        for name in seen_instances:
            cid = inst_to_cid.get(name)
            if cid in monitored and name not in self.tails:
                t = Tail(name, self.store, self.stop_event, self.sashi_bin,
                         spell_manager=self.spell_manager,
                         policy=self.policy,
                         contract_id=cid)
                self.tails[name] = t
                t.start()
                started += 1
                print(f"[discover] tailing {name[:16]} (cluster {cid[:12]})")

        # Reap tails whose cluster is no longer monitored (or whose instance
        # has vanished from `sashi list`).
        for name in list(self.tails.keys()):
            cid = inst_to_cid.get(name, self.tails[name].contract_id)
            keep = (cid in monitored) and (name in seen_instances)
            if not keep:
                try:
                    self.tails[name].shutdown()
                except Exception:
                    pass
                self.tails.pop(name, None)
                reaped += 1
                print(f"[discover] dropped tail {name[:16]} (cluster {(cid or '?')[:12]} unmonitored)")

        return {
            "clusters_seen":  len(seen_clusters),
            "instances_seen": len(seen_instances),
            "tails_started":  started,
            "tails_reaped":   reaped,
        }

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.discover_once()
            except Exception as e:
                print(f"[discover] {e}", file=sys.stderr)
            # Sleep until interval elapses OR a manual trigger wakes us.
            self._wake.clear()
            waited = 0.0
            step = 0.5
            while waited < self.interval and not self.stop_event.is_set() and not self._wake.is_set():
                self.stop_event.wait(step)
                waited += step


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
# Host-metrics collector
# --------------------------------------------------------------------------

class MetricsCollector(threading.Thread):
    """Samples machine + per-HP-process metrics into `host_metrics`.

    Normal cadence: every `normal_interval` s. While an error spell is active
    (signalled via `boost()`), every `boost_interval` s, with rows tagged
    during_spell=1. `sample_now()` takes an immediate out-of-band sample (used
    the instant a spell starts).
    """

    def __init__(self, store: Store, stop_event: threading.Event,
                 instances_ref: dict, normal_interval: int = METRICS_INTERVAL,
                 boost_interval: int = METRICS_INTERVAL_BOOST, ntp_enabled: bool = True):
        super().__init__(daemon=True, name="metrics")
        self.store = store
        self.stop = stop_event
        self.instances_ref = instances_ref
        self.normal_interval = max(2, normal_interval)
        self.boost_interval = max(1, boost_interval)
        self.ntp_enabled = ntp_enabled
        self._lock = threading.Lock()
        self._boost_until = 0.0
        self._boost_spell_id: str | None = None
        self._prev: dict = {}
        self._pid_cache: dict[str, int | None] = {}
        self.tick_cb = None  # set by main() to SpellManager.tick

    def boost(self, spell_id: str, duration: float) -> None:
        with self._lock:
            self._boost_until = max(self._boost_until, time.time() + duration)
            self._boost_spell_id = spell_id

    def _boost_now(self) -> tuple[bool, str | None]:
        with self._lock:
            active = time.time() < self._boost_until
            return active, (self._boost_spell_id if active else None)

    # ---- sample construction ------------------------------------------

    def _build_rows(self, during_spell: int, spell_id: str | None) -> list[dict]:
        now = time.time()
        rows: list[dict] = []
        cpu = _cpu_jiffies(); load = _loadavg(); mem = _meminfo()
        net = _net_totals(); fds = _sys_open_fds()
        ntp_ms, ntp_synced = (_ntp_status() if self.ntp_enabled else (None, None))

        cpu_pct = steal_pct = None
        if cpu and self._prev.get("cpu"):
            i0, t0, s0 = self._prev["cpu"]; i1, t1, s1 = cpu
            dt = t1 - t0
            if dt > 0:
                cpu_pct = round(max(0.0, 100.0 * (1.0 - (i1 - i0) / dt)), 1)
                steal_pct = round(max(0.0, 100.0 * (s1 - s0) / dt), 2)
        net_rx = net_tx = None
        if net and self._prev.get("net") and self._prev.get("ts"):
            r0, x0 = self._prev["net"]; r1, x1 = net
            dts = now - self._prev["ts"]
            if dts > 0:
                net_rx = round(max(0.0, (r1 - r0) / dts / 1024.0), 1)
                net_tx = round(max(0.0, (x1 - x0) / dts / 1024.0), 1)
        self._prev = {"cpu": cpu, "net": net, "ts": now}

        mounts = {m for m in ALWAYS_SAMPLE_MOUNTS if os.path.isdir(m)}
        mounts.add("/")
        disks: dict[str, dict] = {}
        worst_used = worst_inode = 0.0
        min_free: float | None = None
        for m in mounts:
            d = _disk_usage(m)
            if not d:
                continue
            disks[m] = {k: round(v, 1) for k, v in d.items()}
            worst_used = max(worst_used, d["used_pct"])
            worst_inode = max(worst_inode, d["inode_used_pct"])
            min_free = d["free_mb"] if min_free is None else min(min_free, d["free_mb"])

        mem_used_pct = mem_avail_mb = swap_used_mb = None
        if mem:
            total = mem.get("MemTotal", 0)
            avail = mem.get("MemAvailable", mem.get("MemFree", 0))
            if total:
                mem_used_pct = round(100.0 * (1.0 - avail / total), 1)
            mem_avail_mb = round(avail / 1024.0, 1)
            swap_used_mb = round((mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) / 1024.0, 1)

        rows.append({
            "ts": now, "instance": None, "during_spell": during_spell, "spell_id": spell_id,
            "cpu_pct": cpu_pct, "steal_pct": steal_pct,
            "load1": load[0] if load else None, "load5": load[1] if load else None,
            "mem_used_pct": mem_used_pct, "mem_avail_mb": mem_avail_mb,
            "swap_used_mb": swap_used_mb,
            "disk_used_pct": round(worst_used, 1),
            "disk_free_mb": round(min_free, 1) if min_free is not None else None,
            "inode_used_pct": round(worst_inode, 1),
            "net_rx_kbps": net_rx, "net_tx_kbps": net_tx, "sys_open_fds": fds,
            "ntp_offset_ms": (round(ntp_ms, 3) if ntp_ms is not None else None),
            "ntp_synced": ntp_synced,
            "extra": json.dumps({"disks": disks}),
        })

        # Per-instance HP process rows.
        for name in list(self.instances_ref.keys()):
            pid = self._pid_cache.get(name)
            if not pid or not os.path.isdir(f"/proc/{pid}"):
                pid = _find_instance_pid(name)
                self._pid_cache[name] = pid
            if not pid:
                continue
            rss = _proc_rss_mb(pid)
            pfds = _proc_open_fds(pid)
            if rss is None and pfds is None:
                continue
            rows.append({
                "ts": now, "instance": name, "during_spell": during_spell,
                "spell_id": spell_id, "proc_rss_mb": rss, "proc_open_fds": pfds,
                "proc_pid": pid,
            })
        return rows

    def sample_now(self, spell_id: str | None = None, during_spell: int = 0) -> None:
        try:
            rows = self._build_rows(during_spell, spell_id)
            self.store.insert_host_metrics(rows)
        except Exception as e:
            print(f"[metrics] sample failed: {e}", file=sys.stderr)

    def run(self) -> None:
        # Prime the deltas so the first real sample has rates.
        self._prev = {"cpu": _cpu_jiffies(), "net": _net_totals(), "ts": time.time()}
        while not self.stop.is_set():
            boosting, sid = self._boost_now()
            self.sample_now(spell_id=sid, during_spell=1 if boosting else 0)
            if callable(self.tick_cb):
                try:
                    self.tick_cb()
                except Exception as e:
                    print(f"[metrics] tick: {e}", file=sys.stderr)
            self.stop.wait(self.boost_interval if boosting else self.normal_interval)


# --------------------------------------------------------------------------
# Error-spell manager + diagnostic snapshots
# --------------------------------------------------------------------------

def capture_snapshot(store: Store, spell_id: str, sashi_bin: str,
                     instances: list[str]) -> None:
    """Capture machine-wide + per-instance diagnostics into `spell_artifacts`."""
    ts = time.time()

    def add(kind: str, content: str | None, inst: str | None = None) -> None:
        store.add_spell_artifact(spell_id, inst, kind, content, ts)

    add("ps",         run_cmd(["sh", "-c", "ps aux --sort=-%mem 2>/dev/null | head -n 30"], timeout=8))
    add("free",       run_cmd(["sh", "-c", "free -m 2>/dev/null; echo; head -n 6 /proc/meminfo"], timeout=5))
    add("df",         run_cmd(["sh", "-c", "df -h 2>/dev/null"], timeout=8))
    add("dfi",        run_cmd(["sh", "-c", "df -i 2>/dev/null"], timeout=8))
    add("uptime",     run_cmd(["sh", "-c", "uptime; echo; cat /proc/loadavg; echo; cat /proc/stat | head -n 1"], timeout=5))
    add("vmstat",     run_cmd(["sh", "-c", "vmstat 1 3 2>/dev/null || (echo 'vmstat unavailable'; cat /proc/stat | head -n 1)"], timeout=8))
    add("dmesg",      run_cmd(["sh", "-c", "(dmesg --time-format=iso 2>/dev/null || dmesg 2>/dev/null) | tail -n 80"], timeout=8))
    # journalctl: a few angles — recent system-wide, the sashimono agent unit,
    # and any unit whose name contains 'sashi'/'hp'/'evernode' (covers the
    # per-instance services regardless of exact naming).
    add("journalctl", run_cmd(["sh", "-c",
        "journalctl --since '-5 min' -n 900 --no-pager 2>/dev/null || echo 'journalctl unavailable'"], timeout=14))
    add("journalctl_agent", run_cmd(["sh", "-c",
        "for u in sashimono-agent.service sashimono.service sashi-agent.service mb-xrpl.service; do "
        "  echo \"=== $u ===\"; journalctl -u \"$u\" -n 300 --no-pager 2>/dev/null || echo '(no such unit)'; echo; "
        "done"], timeout=14))
    add("journalctl_units", run_cmd(["sh", "-c",
        "for u in $(systemctl list-units --no-legend --plain 'sashi*' 'hp*' 'evernode*' '*sashimono*' 2>/dev/null | awk '{print $1}' | sort -u); do "
        "  echo \"=== $u ===\"; journalctl -u \"$u\" -n 200 --no-pager 2>/dev/null; echo; "
        "done | tail -n 900 || echo 'no matching units / not systemd'"], timeout=18))
    add("chronyc",    run_cmd(["sh", "-c", "chronyc tracking 2>/dev/null; echo '---'; chronyc sources 2>/dev/null; echo '---'; timedatectl 2>/dev/null"], timeout=8))
    add("du",         run_cmd(["sh", "-c", "du -sh /var/lib/sashimono/* 2>/dev/null; du -sh /home/sashi*/.* 2>/dev/null | tail -n 40; du -sh /home/sashi* 2>/dev/null"], timeout=25))

    for name in (instances or []):
        try:
            lines = store.recent_log_lines(name, limit=200)
            txt = "\n".join(
                f"{datetime.fromtimestamp(l['ts'], timezone.utc).isoformat()} "
                f"[{l.get('level')}][{l.get('module')}] ({l.get('tag')}) {l.get('msg')}"
                for l in lines
            )
            add("logtail", txt or "(no recent log lines captured)", inst=name)
        except Exception:
            pass


class SpellManager:
    """Tracks per-instance error-spell state from the event stream and, on the
    transition *into* a spell, kicks off a metrics boost + a diagnostic snapshot
    burst. Closes a spell (recovered) on the next `ledger_created`."""

    def __init__(self, store: Store, stop_event: threading.Event, sashi_bin: str,
                 metrics: MetricsCollector | None, instances_ref: dict,
                 boost_cooldown: float = METRICS_BOOST_COOLDOWN,
                 recapture_s: float = SNAPSHOT_RECAPTURE,
                 max_captures: int = SNAPSHOT_MAX_CAPTURES,
                 policy: "PolicyManager | None" = None):
        self.store = store
        self.stop = stop_event
        self.sashi_bin = sashi_bin
        self.metrics = metrics
        self.instances_ref = instances_ref
        self.boost_cooldown = boost_cooldown
        self.recapture_s = recapture_s
        self.max_captures = max_captures
        self.policy = policy
        self.lock = threading.Lock()
        self.state: dict[str, dict] = {}
        # Re-attach to spells left open by a previous run.
        try:
            for r in store.open_spells():
                self.state[r["instance"]] = {
                    "in_spell": True, "spell_id": r["spell_id"],
                    "start_ts": r["start_ts"], "last_error_ts": r["start_ts"],
                    "last_ledger_ts": 0.0,
                    "severity": tag_severity(r.get("trigger_tag", "")),
                    "trigger_tag": r.get("trigger_tag"),
                }
        except Exception:
            pass

    def current_spell(self, instance: str) -> dict | None:
        """Return a snapshot of the active spell for `instance`, or None.
        Used by PolicyManager to decide whether to gate event inserts."""
        with self.lock:
            s = self.state.get(instance)
            if not s or not s.get("in_spell"):
                return None
            return {
                "spell_id":    s.get("spell_id"),
                "severity":    s.get("severity", "high"),
                "trigger_tag": s.get("trigger_tag"),
                "start_ts":    s.get("start_ts"),
            }

    def _st(self, instance: str) -> dict:
        return self.state.setdefault(instance, {
            "in_spell": False, "spell_id": None, "start_ts": 0.0,
            "last_error_ts": 0.0, "last_ledger_ts": 0.0,
        })

    def on_event(self, instance: str, ev: dict) -> None:
        tag = ev.get("tag")
        if tag not in ERROR_TAGS and tag != "ledger_created":
            return
        ts = ev.get("ts") or time.time()
        with self.lock:
            s = self._st(instance)
            if tag == "ledger_created":
                s["last_ledger_ts"] = ts
                if s["in_spell"]:
                    sid = s["spell_id"]
                    s["in_spell"] = False
                    s["spell_id"] = None
                    self.store.close_spell(sid, ts, recovered=1)
                    print(f"[spell] {instance[:16]}: recovered ({sid})")
                return
            # error tag
            s["last_error_ts"] = ts
            if s["in_spell"]:
                return
            sid = f"{instance}-{int(ts)}"
            severity = tag_severity(tag)
            s["in_spell"] = True
            s["spell_id"] = sid
            s["start_ts"] = ts
            s["severity"] = severity
            s["trigger_tag"] = tag
            self.store.open_spell(sid, instance, ts, tag, ev.get("msg", ""))
            actions = (self.policy.spell_actions(severity)
                       if self.policy is not None
                       else {"boost": True, "snapshot": True, "events": True})
            print(f"[spell] {instance[:16]}: ENTERED spell {sid} [{tag}] "
                  f"severity={severity} actions={actions} "
                  f"{(ev.get('msg') or '')[:90]}")
            if self.metrics and actions.get("boost", True):
                self.metrics.boost(sid, self.boost_cooldown)
                self.metrics.sample_now(spell_id=sid, during_spell=1)
            if actions.get("snapshot", True):
                threading.Thread(target=self._snapshot_loop, args=(sid, instance),
                                 daemon=True, name=f"snap-{sid[-12:]}").start()

    def is_active(self, spell_id: str) -> bool:
        with self.lock:
            return any(s["in_spell"] and s["spell_id"] == spell_id
                       for s in self.state.values())

    def tick(self) -> None:
        """If a spell had no error for boost_cooldown and a ledger arrived after
        it started, close it (recovered). Open spells with no ledger since stay
        open — a query treats those older than HARD_FORK_AFTER as hard forks."""
        now = time.time()
        with self.lock:
            for inst, s in self.state.items():
                if (s["in_spell"] and now - s["last_error_ts"] > self.boost_cooldown
                        and s["last_ledger_ts"] > s["start_ts"]):
                    sid = s["spell_id"]
                    s["in_spell"] = False
                    s["spell_id"] = None
                    self.store.close_spell(sid, s["last_ledger_ts"], recovered=1)
                    print(f"[spell] {inst[:16]}: closed late ({sid})")

    def _instances(self) -> list[str]:
        names = set(self.instances_ref.keys()) | set(self.state.keys())
        return sorted(names)

    def _snapshot_loop(self, spell_id: str, instance: str) -> None:
        captures = 0
        while captures < self.max_captures and not self.stop.is_set():
            try:
                capture_snapshot(self.store, spell_id, self.sashi_bin, self._instances())
                self.store.spell_inc_captures(spell_id)
            except Exception as e:
                print(f"[snapshot {spell_id}] {e}", file=sys.stderr)
            captures += 1
            waited = 0.0
            while waited < self.recapture_s and not self.stop.is_set():
                self.stop.wait(min(5.0, self.recapture_s - waited))
                waited += 5.0
                if not self.is_active(spell_id):
                    return
            if not self.is_active(spell_id):
                return


# --------------------------------------------------------------------------
# Comprehensive text report (for hand-off to an analyst / LLM)
# --------------------------------------------------------------------------
#
# One plain-text document covering every HotPocket instance this monitor sees,
# focused on error spells: spell metadata, the HotPocket/contract log around
# the spell start, host metrics around it (including the 3s boosted samples),
# and the captured journalctl/dmesg/ps/df/... snapshots. If --report-peers is
# set, the report from each peer monitor is fetched and appended, so a multi-VM
# cluster produces one document covering all nodes.

_RULE = "=" * 80
_RULE2 = "#" * 78


def _iso(ts):
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except Exception:
        return str(ts)


def _agewords(s):
    if s is None:
        return "—"
    s = float(s)
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s/60:.1f}m"
    if s < 86400:
        return f"{s/3600:.1f}h"
    return f"{s/86400:.1f}d"


def _clip(text, max_chars=40000):
    if text is None:
        return "(none)"
    text = str(text)
    if len(text) <= max_chars:
        return text
    head = text[: max_chars - 200]
    return head + f"\n... [truncated — {len(text)-len(head)} more chars; full content is in the monitor DB / dashboard] ..."


def _metrics_table(rows):
    """rows = host_metrics rows (ASC). Returns an aligned text table."""
    if not rows:
        return "(no host-metric samples in this window — metrics disabled, or none recorded)"
    cols = [
        ("ts (UTC)", lambda r: _iso(r["ts"]), 26),
        ("B", lambda r: "B" if r.get("during_spell") else "", 1),
        ("cpu%", lambda r: _fmtnum(r.get("cpu_pct")), 6),
        ("steal%", lambda r: _fmtnum(r.get("steal_pct")), 7),
        ("load1", lambda r: _fmtnum(r.get("load1")), 6),
        ("mem%", lambda r: _fmtnum(r.get("mem_used_pct")), 6),
        ("memMB", lambda r: _fmtnum(r.get("mem_avail_mb"), 0), 8),
        ("swapMB", lambda r: _fmtnum(r.get("swap_used_mb"), 0), 8),
        ("diskFreeMB", lambda r: _fmtnum(r.get("disk_free_mb"), 0), 11),
        ("inode%", lambda r: _fmtnum(r.get("inode_used_pct")), 7),
        ("rxKB", lambda r: _fmtnum(r.get("net_rx_kbps"), 0), 7),
        ("txKB", lambda r: _fmtnum(r.get("net_tx_kbps"), 0), 7),
        ("sysFds", lambda r: _fmtnum(r.get("sys_open_fds"), 0), 8),
        ("ntpMs", lambda r: _fmtnum(r.get("ntp_offset_ms")), 8),
        ("sync", lambda r: ("" if r.get("ntp_synced") is None else str(int(r["ntp_synced"]))), 4),
    ]
    out = ["  ".join(name.ljust(w) for name, _, w in cols)]
    for r in rows:
        out.append("  ".join(str(fn(r)).ljust(w) for name, fn, w in cols))
    return "\n".join(out)


def _proc_metrics_table(rows):
    rows = [r for r in rows if r.get("instance") is not None]
    if not rows:
        return "(no per-process samples — HP process not found, or metrics disabled)"
    cols = [
        ("ts (UTC)", lambda r: _iso(r["ts"]), 26),
        ("B", lambda r: "B" if r.get("during_spell") else "", 1),
        ("instance", lambda r: str(r.get("instance") or "")[:24], 24),
        ("rss_mb", lambda r: _fmtnum(r.get("proc_rss_mb"), 0), 8),
        ("open_fds", lambda r: _fmtnum(r.get("proc_open_fds"), 0), 9),
        ("pid", lambda r: _fmtnum(r.get("proc_pid"), 0), 8),
    ]
    out = ["  ".join(name.ljust(w) for name, _, w in cols)]
    for r in rows:
        out.append("  ".join(str(fn(r)).ljust(w) for name, fn, w in cols))
    return "\n".join(out)


def _fmtnum(x, d=1):
    if x is None:
        return ""
    try:
        x = float(x)
    except Exception:
        return str(x)
    if abs(x) >= 1000 or d == 0:
        return str(int(round(x)))
    return f"{x:.{d}f}"


def _events_block(rows):
    if not rows:
        return "(no log events captured for this instance in this window)"
    return "\n".join(
        f"{_iso(r['ts'])} [{r.get('level') or '?'}][{r.get('module') or '?'}] ({r.get('tag') or '?'}) {r.get('msg') or ''}"
        for r in rows
    )


# Order in which artifact kinds are printed within a capture burst.
_REPORT_ARTIFACT_ORDER = [
    "logtail", "journalctl", "journalctl_agent", "journalctl_units", "dmesg",
    "ps", "free", "vmstat", "uptime", "df", "dfi", "chronyc", "du",
]


def _fetch_peer_report(url, window):
    """Fetch /api/report?self=1 from a peer monitor. Returns text or an error note."""
    u = url.rstrip("/") + f"/api/report?self=1&window={window}"
    try:
        req = urllib.request.Request(u, headers={"User-Agent": "sashimon-report"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return f"\n{_RULE}\nPEER {url}: UNREACHABLE — {e}\n{_RULE}\n"


def build_report(store: Store, window_seconds: int, sashi_bin: str,
                 hostname: str, report_peers=None, include_peers=True,
                 roundtime_ms: int = DEFAULT_ROUNDTIME_MS) -> str:
    now = time.time()
    all_time = window_seconds <= 0
    since = 0.0 if all_time else now - window_seconds
    span_txt = "all recorded history" if all_time else f"last {window_seconds}s (since {_iso(since)})"
    sashi_ver = (run_cmd([sashi_bin, "version"], timeout=4) or run_cmd([sashi_bin, "--version"], timeout=4) or "").strip().splitlines()
    sashi_ver = sashi_ver[0] if sashi_ver else "(unknown)"

    instances = store.list_instances()
    summ = {s["name"]: s for s in store.summary(window_seconds, roundtime_ms)}
    L = []
    L.append(_RULE)
    L.append("SASHIMON ERROR-SPELL REPORT")
    L.append(f"generated   : {_iso(now)} (epoch {now:.0f})")
    L.append(f"monitor host: {hostname}")
    L.append(f"sashi       : {sashi_bin}   version: {sashi_ver}")
    L.append(f"roundtime   : {roundtime_ms} ms (configured in this monitor; the cluster's effective value is in the HotPocket logs / 'getledger')")
    L.append(f"window      : {span_txt}")
    L.append(f"instances on this VM ({len(instances)}): {', '.join(i['name'] for i in instances) or '(none)'}")
    L.append("")
    L.append("NOTE: each sashimon instance sees only the HotPocket instances on its own VM. If your cluster")
    L.append("spans multiple VMs, either run 'export report' on each VM and concatenate the files, or start")
    L.append("the monitors with --report-peers so this export fetches and appends the others.")
    if report_peers and include_peers:
        L.append(f"report-peers configured: {', '.join(report_peers)} (their sections are appended below)")
    L.append(_RULE)
    L.append("")
    L.append("READING GUIDE")
    L.append("  - 'No consensus on last shard hash. won:X needed:Y' / 'Cannot close ledger. Possible fork")
    L.append("    condition' = a vote split. Recoverable if one side >= ceil(threshold% x UNL); a hard fork")
    L.append("    (no recovery) when no side reaches that, so look at X vs Y and which nodes are odd-one-out.")
    L.append("  - For each spell below: the HotPocket/contract log just before & during it, host metrics around")
    L.append("    it (rows marked 'B' are the 3-second boosted samples taken while the spell was active), and")
    L.append("    the captured journalctl/dmesg/ps/df/chronyc/du/contract-log snapshots.")
    L.append("  - Cross-correlate by the UTC timestamps + epoch seconds. Suspects for an unrecoverable hard")
    L.append("    fork on a small cluster: a 2nd node faulting while a 1st is recovering; disk full (ledger")
    L.append("    persist fails); OOM/restart; clock drift > 2x roundtime; CPU steal; a contract determinism")
    L.append("    bug (different output/state hash).")
    L.append("")

    for inst in instances:
        name = inst["name"]
        s = summ.get(name, {})
        c = s.get("counts", {}) or {}
        latest = None
        try:
            hm = store.host_metrics_window(None, now - 600, now)   # last 10 min, machine rows
            latest = next((r for r in hm if r.get("instance") is None), None)
        except Exception:
            latest = None
        latest_proc = None
        try:
            pm = store.host_metrics_window(name, now - 600, now)
            latest_proc = next((r for r in pm if r.get("instance") == name), None)
        except Exception:
            latest_proc = None

        L.append("")
        L.append(_RULE2)
        L.append(f"# INSTANCE: {name}")
        L.append(f"#   contract_id={inst.get('contract_id')}  tenant={inst.get('tenant')}  image={inst.get('image')}")
        L.append(f"#   user_port={inst.get('user_port')}  peer_port={inst.get('peer_port')}  sashi_status={inst.get('status')}")
        L.append(f"#   health={s.get('health','?')}  uptime_est(window)={s.get('uptime_pct','?')}%  "
                 f"last_ledger={_agewords(s.get('last_ledger_age_s'))} ago  last_event={_agewords(s.get('last_event_age_s'))} ago")
        L.append(f"#   event counts (window): " + (", ".join(f"{k}={v}" for k, v in sorted(c.items())) or "(none)"))
        if latest:
            L.append(f"#   latest host metrics (this VM, machine) @ {_iso(latest['ts'])}: "
                     f"cpu={_fmtnum(latest.get('cpu_pct'))}% steal={_fmtnum(latest.get('steal_pct'))}% load1={_fmtnum(latest.get('load1'))} "
                     f"mem={_fmtnum(latest.get('mem_used_pct'))}% memAvail={_fmtnum(latest.get('mem_avail_mb'),0)}MB "
                     f"diskFree={_fmtnum(latest.get('disk_free_mb'),0)}MB inode={_fmtnum(latest.get('inode_used_pct'))}% "
                     f"ntp={_fmtnum(latest.get('ntp_offset_ms'))}ms sync={latest.get('ntp_synced')} extra={latest.get('extra')}")
        if latest_proc:
            L.append(f"#   latest HP process @ {_iso(latest_proc['ts'])}: rss={_fmtnum(latest_proc.get('proc_rss_mb'),0)}MB "
                     f"open_fds={_fmtnum(latest_proc.get('proc_open_fds'),0)} pid={latest_proc.get('proc_pid')}")
        L.append(_RULE2)

        # recent context: last 60 events (all tags)
        L.append("")
        L.append(f"----- recent log events on {name} (last 60, all tags) -----")
        L.append(_events_block(store.recent_log_lines(name, limit=60)))

        # spells for this instance, oldest first (so the doc reads chronologically)
        try:
            spells = sorted(store.spells_log_window(since, now, name, limit=1000), key=lambda x: x["start_ts"])
        except Exception as e:
            spells = []
            L.append(f"(error loading spells: {e})")
        if not spells:
            L.append("")
            L.append(f"(no error spells recorded for {name} in this window)")
        for sp in spells:
            sid = sp["spell_id"]
            st0 = float(sp["start_ts"])
            st1 = float(sp["end_ts"]) if sp.get("end_ts") else now
            ev0, ev1 = st0 - 300, st1 + 60          # HotPocket log: 5 min before .. 1 min after
            m0, m1 = st0 - 180, st1 + 60            # host metrics: 3 min before .. 1 min after
            L.append("")
            L.append("=" * 80)
            L.append(f"ERROR SPELL  {sid}")
            L.append(f"  instance : {name}")
            L.append(f"  state    : {sp.get('state')}   recovered: {'yes' if sp.get('recovered') else ('no — STILL OPEN' if sp.get('end_ts') is None else 'no')}")
            L.append(f"  start    : {_iso(st0)} (epoch {st0:.0f})")
            L.append(f"  end      : {('(open, ' + _agewords(now - st0) + ' so far)') if sp.get('end_ts') is None else _iso(sp['end_ts']) + ' (epoch %.0f)' % float(sp['end_ts'])}")
            L.append(f"  duration : {_agewords(sp.get('duration_s'))}")
            L.append(f"  trigger  : {sp.get('trigger_tag')}  ::  {sp.get('trigger_msg')}")
            L.append(f"  captures : {sp.get('captures', 0)}")
            L.append("=" * 80)

            try:
                evs = sorted(store.events_window(name, ev0, ev1, None, 5000), key=lambda x: x["ts"])
            except Exception as e:
                evs = []
                L.append(f"(error loading events: {e})")
            L.append("")
            L.append(f"--- HotPocket / contract log around the spell  ({name}, {_iso(ev0)} .. {_iso(ev1)}, {len(evs)} lines) ---")
            L.append(_events_block(evs))

            try:
                hmw = sorted(store.host_metrics_window(None, m0, m1, 20000), key=lambda x: (x["ts"], 0 if x.get("instance") is None else 1))
            except Exception as e:
                hmw = []
                L.append(f"(error loading host metrics: {e})")
            mach_rows = [r for r in hmw if r.get("instance") is None]
            proc_rows = [r for r in hmw if r.get("instance") is not None]
            boosted_n = sum(1 for r in hmw if r.get("during_spell"))
            L.append("")
            L.append(f"--- host metrics around the spell  (machine, {_iso(m0)} .. {_iso(m1)}, {len(mach_rows)} samples, {boosted_n} boosted) ---")
            L.append(_metrics_table(mach_rows))
            if proc_rows:
                L.append("")
                L.append(f"--- per-process metrics around the spell  ({len(proc_rows)} samples) ---")
                L.append(_proc_metrics_table(proc_rows))

            # captured snapshots, grouped by capture burst
            try:
                arts = store.spell_artifacts(sid)
            except Exception as e:
                arts = []
                L.append(f"(error loading artifacts: {e})")
            if not arts:
                L.append("")
                L.append("--- captured snapshots: none (spell may be new, or capture tools unavailable on this host) ---")
            else:
                arts_sorted = sorted(arts, key=lambda a: (a["ts"], _REPORT_ARTIFACT_ORDER.index(a["kind"]) if a["kind"] in _REPORT_ARTIFACT_ORDER else 99))
                groups = []
                for a in arts_sorted:
                    g = next((g for g in groups if abs(g["ts"] - a["ts"]) < 5), None)
                    if not g:
                        g = {"ts": a["ts"], "items": []}
                        groups.append(g)
                    g["items"].append(a)
                for gi, g in enumerate(groups):
                    L.append("")
                    L.append(f"--- captured snapshot {gi+1}/{len(groups)}  @ {_iso(g['ts'])} ---")
                    for a in g["items"]:
                        ins = f"  ({a.get('instance')})" if a.get("instance") else ""
                        L.append("")
                        L.append(f"[{a['kind']}{ins}  captured {_iso(a['ts'])}]")
                        L.append(_clip(a.get("content")))
        L.append("")

    L.append("")
    L.append(_RULE)
    L.append(f"END OF REPORT — {hostname}")
    L.append(_RULE)

    text = "\n".join(L) + "\n"

    if report_peers and include_peers:
        for peer in report_peers:
            text += "\n\n" + ("#" * 80) + f"\n# PEER MONITOR: {peer}\n" + ("#" * 80) + "\n\n"
            text += _fetch_peer_report(peer, window_seconds)

    return text


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
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  /* sashi.mon — control-room console. dark, monospace-forward, amber accent,
     red for fork conditions. data is the hero; chrome is hairlines. */
  :root {
    color-scheme: dark;
    --bg: #07090c; --bg1: #0d1117; --bg2: #11161d; --bg3: #161c25;
    --line: #232c38; --line2: #2f3a48;
    --fg: #c9d4e0; --fg-dim: #6b7989; --fg-faint: #4a5765;
    --ok: #3fb950; --ok-dim: #1f7a33;
    --warn: #e0a82e; --warn-dim: #8a6512;
    --bad: #ff5a52; --bad-dim: #8f2420;
    --info: #58a6ff; --accent: #e0a82e;
    --mono: 'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
    --sans: 'IBM Plex Sans', -apple-system, Segoe UI, Roboto, sans-serif;
  }
  * { box-sizing: border-box; }
  body { font-family: var(--sans); margin: 0; background: var(--bg); color: var(--fg);
         -webkit-font-smoothing: antialiased;
         background-image:
           radial-gradient(900px 400px at 90% -10%, rgba(224,168,46,.05), transparent 60%),
           repeating-linear-gradient(0deg, rgba(255,255,255,.012) 0 1px, transparent 1px 3px);
  }
  ::selection { background: rgba(224,168,46,.3); }
  header { padding: 12px 22px; background: var(--bg1);
           border-bottom: 1px solid var(--line);
           display: flex; align-items: baseline; gap: 14px; position: sticky; top: 0; z-index: 50;
           box-shadow: 0 1px 0 rgba(0,0,0,.5); }
  header h1 { margin: 0; font-family: var(--mono); font-size: 16px; font-weight: 700;
              letter-spacing: .06em; color: var(--fg); }
  header h1 .dot { color: var(--accent); }
  header .meta { font-size: 11px; color: var(--fg-dim); font-family: var(--mono); }
  header .live { margin-left: auto; font-family: var(--mono); font-size: 10px;
                 color: var(--fg-dim); display: flex; align-items: center; gap: 6px; }
  header .live::before { content:""; width:6px; height:6px; border-radius:50%; background: var(--ok);
                         box-shadow: 0 0 6px var(--ok); animation: pulse 2.2s ease-in-out infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.25} }
  main { padding: 18px 22px 60px; max-width: 1480px; margin: 0 auto; }
  .controls { display: flex; gap: 10px; align-items: center; margin-bottom: 18px;
              font-family: var(--mono); font-size: 11px; color: var(--fg-dim); }
  .controls label { display:flex; align-items:center; gap:6px; text-transform:uppercase; letter-spacing:.05em; }
  select, button { background: var(--bg2); color: var(--fg); border: 1px solid var(--line);
                   padding: 5px 10px; font-family: var(--mono); font-size: 11px; border-radius: 3px; }
  button { cursor: pointer; }
  button:hover, select:hover { border-color: var(--line2); }
  button.danger { color: var(--bad); border-color: var(--bad-dim); }
  button.danger .dbsz { color: var(--fg-faint); font-weight: 500; margin-left: 4px;
                        font-variant-numeric: tabular-nums; }
  button.danger:hover .dbsz { color: var(--bad-dim); }
  .policy-ctl { display:inline-flex; align-items:center; gap:6px; padding:0;
                color: var(--fg-dim); font-family: var(--mono); font-size: 10.5px;
                text-transform: uppercase; letter-spacing: .05em; }
  .policy-ctl select { color: var(--accent); border-color: var(--line2);
                       text-transform: lowercase; letter-spacing: 0; }
  .policy-ctl select:disabled { opacity: .55; cursor: progress; }

  /* --- cluster picker + filter --- */
  #clusterPanel .pb { padding: 12px; }
  .ph { display:flex; align-items:center; gap:10px; }
  .clist { display:grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap:8px; }
  .crow { display:flex; align-items:stretch; gap:0; background: var(--bg2);
          border:1px solid var(--line); border-radius:5px; overflow:hidden;
          font-family: var(--mono); transition: border-color .12s; }
  .crow.mon { border-color: var(--ok-dim); }
  .crow.act { border-color: var(--ok); box-shadow: inset 0 0 0 1px var(--ok); }
  .crow .tg { flex:0 0 auto; display:flex; align-items:center; padding:0 12px;
              background: var(--bg3); border-right:1px solid var(--line); cursor:pointer; }
  .crow .tg input { display:none; }
  .crow .tg .sw { width:30px; height:16px; border-radius:999px; background: var(--line);
                  position:relative; transition: background .15s; }
  .crow .tg .sw::after { content:''; position:absolute; left:2px; top:2px; width:12px; height:12px;
                         border-radius:50%; background: var(--fg-dim); transition: transform .15s, background .15s; }
  .crow .tg input:checked + .sw { background: var(--ok-dim); }
  .crow .tg input:checked + .sw::after { transform: translateX(14px); background: var(--ok); }
  .crow .info { flex:1 1 auto; padding:9px 12px; min-width:0; cursor:pointer; }
  .crow .info:hover { background: rgba(255,255,255,.02); }
  .crow .info:disabled, .crow .info.dis { cursor: default; opacity: .55; }
  .crow .id-row { display:flex; align-items:center; gap:8px; flex-wrap:wrap;
                  font-size:11px; font-weight:600; color: var(--fg); }
  .crow .id-row .cid { background:var(--bg); padding:1px 6px; border-radius:3px;
                       font-size:10.5px; color:var(--fg); }
  .crow .id-row .np  { font-size:9.5px; font-weight:700; padding:1px 7px; border-radius:999px;
                       background: rgba(91,102,122,.25); color: var(--fg-dim); }
  .crow .id-row .np.hot { background: rgba(63,185,80,.18); color: var(--ok); }
  .crow .meta-row { margin-top:3px; font-size:10px; color:var(--fg-dim); display:flex; gap:4px 12px; flex-wrap:wrap; }
  .crow .meta-row b { font-size:9px; color:var(--fg-faint); text-transform:uppercase;
                      letter-spacing:.05em; font-weight:700; margin-right:3px; }
  .all-row { grid-column: 1 / -1; }
  .all-row .info::before { content:"●"; color: var(--ok); margin-right:8px; }
  .cluster-banner { display:flex; align-items:center; gap:10px; flex-wrap:wrap;
                    padding:8px 12px; margin-bottom:14px;
                    background: rgba(63,185,80,.06);
                    border:1px solid var(--ok-dim); border-radius:4px;
                    font-family: var(--mono); font-size: 11px; }
  .cluster-banner .lbl { font-size:9px; color:var(--fg-dim); text-transform:uppercase;
                         letter-spacing:.07em; }
  .cluster-banner .cid { background: var(--bg); padding:2px 7px; border-radius:3px;
                         color: var(--fg); word-break: break-all; }
  .cluster-banner .clr { margin-left:auto; cursor:pointer; background:none;
                         border:1px solid var(--line); color: var(--fg-dim);
                         padding:2px 9px; border-radius:3px; font-family:inherit; font-size:10px; }
  .cluster-banner .clr:hover { color: var(--fg); border-color: var(--line2); }
  .panel { margin-bottom: 18px; background: var(--bg1); border: 1px solid var(--line);
           border-radius: 4px; }
  .panel > .ph { padding: 9px 14px; border-bottom: 1px solid var(--line);
                 display:flex; align-items:baseline; gap:10px; }
  .panel > .ph h2 { margin:0; font-family: var(--mono); font-size: 12px; font-weight: 600;
                    letter-spacing:.08em; text-transform: uppercase; color: var(--fg); }
  .panel > .ph .hint { font-family: var(--mono); font-size:10px; color: var(--fg-dim); }
  .panel > .pb { padding: 14px; }
  /* --- tag filter chips (above the error-events chart) --- */
  .tagchips { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px; font-family:var(--mono); font-size:10px; align-items:center; }
  .tagchips .lbl { color:var(--fg-faint); text-transform:uppercase; letter-spacing:.06em; margin-right:2px; }
  .tagchips .c { cursor:pointer; padding:2px 8px; border-radius:2px; border:1px solid var(--line);
                 color:var(--fg-dim); background:transparent; user-select:none; display:inline-flex; align-items:center; gap:5px; }
  .tagchips .c::before { content:""; width:8px; height:8px; border-radius:2px; background:currentColor; opacity:.35; }
  .tagchips .c.on { color:var(--cc, var(--fg)); border-color:currentColor; background:rgba(255,255,255,.04); }
  .tagchips .c.on::before { opacity:1; }
  .tagchips .c:hover { border-color:var(--line2); }
  .tagchips .q { cursor:pointer; padding:2px 8px; border-radius:2px; border:1px solid var(--line); color:var(--fg-dim); background:var(--bg2); }
  .tagchips .q:hover { color:var(--fg); border-color:var(--line2); }
  .tagchips .q.danger { color:var(--bad); border-color:var(--bad-dim); }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 14px; }
  .card { background: var(--bg1); border: 1px solid var(--line); border-radius: 4px; padding: 12px 14px; }
  .card h2 { margin: 0 0 4px; font-size: 12px; font-family: var(--mono); word-break: break-all; }
  .card .sub { font-size: 11px; color: var(--fg-dim); margin-bottom: 10px; font-family: var(--mono); }
  .health, .pill { display: inline-block; padding: 1px 7px; font-family: var(--mono);
            font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing:.06em;
            border:1px solid currentColor; border-radius: 2px; }
  .h-healthy, .pill.ok       { color: var(--ok); }
  .h-consensus_loss, .pill.warn { color: var(--warn); }
  .h-forked, .pill.bad       { color: var(--bad); }
  .h-stalled                 { color: #b083ff; }
  .h-unknown, .pill.dim      { color: var(--fg-dim); }
  .stats { display: grid; grid-template-columns: 1fr 1fr; gap: 3px 14px;
           font-size: 11px; margin-bottom: 10px; font-family: var(--mono); }
  .stats span:nth-child(odd) { color: var(--fg-dim); text-transform:uppercase; letter-spacing:.04em; }
  canvas { width: 100% !important; height: 88px !important; }
  .global canvas { height: 200px !important; }
  .empty { text-align: center; color: var(--fg-dim); padding: 50px 0; font-family: var(--mono); }

  /* --- per-instance card: current metrics + spell list --- */
  .card .pmet { font-family: var(--mono); font-size: 10.5px; color: var(--fg-dim);
                display: flex; flex-wrap: wrap; gap: 4px 12px; margin: 8px 0 4px;
                padding: 6px 8px; background: var(--bg); border: 1px solid var(--line); border-radius: 3px; }
  .card .pmet b { color: var(--fg); font-weight: 600; }
  .card .pmet b.alert { color: var(--bad); }
  .card .pmet .lbl { color: var(--fg-faint); text-transform: uppercase; letter-spacing: .04em; }
  .card .cspells { margin-top: 8px; }
  .card .cspells .hdr { font-family: var(--mono); font-size: 9.5px; text-transform: uppercase;
                        letter-spacing: .06em; color: var(--fg-dim); margin-bottom: 4px; }
  .card .sp { display: flex; align-items: center; gap: 8px; font-family: var(--mono); font-size: 10.5px;
              padding: 3px 6px; border: 1px solid var(--line); border-radius: 3px; margin-bottom: 3px;
              cursor: pointer; transition: background .1s, border-color .1s; }
  .card .sp:hover { background: var(--bg2); border-color: var(--line2); }
  .card .sp::before { content: "\25CF"; }
  .card .sp.recovered::before { color: var(--warn); }
  .card .sp.active::before    { color: var(--bad); }
  .card .sp.hardfork::before  { color: var(--bad); text-shadow: 0 0 6px var(--bad-dim); }
  .card .sp.ended::before     { color: var(--fg-faint); }
  .card .sp .st  { flex: 0 0 70px; color: var(--fg); }
  .card .sp .tg  { color: var(--fg-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .card .sp .tm  { margin-left: auto; color: var(--fg-faint); white-space: nowrap; }
  .card .sp.noclick { cursor: default; color: var(--fg-faint); }
  .card .sp.noclick:hover { background: transparent; border-color: var(--line); }
  .card .sp.noclick::before { content: ""; }
  .card .cspells .more { font-family: var(--mono); font-size: 9.5px; color: var(--fg-faint);
                         cursor: pointer; margin-top: 2px; }
  .card .cspells .more:hover { color: var(--fg-dim); text-decoration: underline; }

  /* --- error-spell timeline ribbon --- */
  .ribbon { position:relative; height: 46px; background:
              repeating-linear-gradient(90deg, var(--line) 0 1px, transparent 1px 25%);
            border:1px solid var(--line); border-radius:3px; overflow:hidden; }
  .ribbon .now { position:absolute; right:0; top:0; bottom:0; width:1px; background: var(--accent); }
  .ribbon .blk { position:absolute; top:6px; bottom:6px; min-width:3px; border-radius:2px;
                 cursor:pointer; opacity:.88; transition: opacity .12s, transform .12s; }
  .ribbon .blk:hover { opacity:1; transform: scaleY(1.12); }
  .ribbon .blk.recovered { background: var(--warn); }
  .ribbon .blk.active    { background: var(--bad); animation: pulse 1.4s ease-in-out infinite; }
  .ribbon .blk.hardfork  { background: var(--bad); box-shadow: 0 0 0 1px var(--bad), 0 0 8px var(--bad-dim) inset; }
  .ribbon .blk.ended     { background: var(--fg-faint); }
  .ribbon .axl { position:absolute; bottom:1px; font-family:var(--mono); font-size:9px; color:var(--fg-faint); }
  .ribbon-legend { font-family:var(--mono); font-size:9px; color:var(--fg-dim); margin-top:5px;
                   display:flex; gap:14px; }
  .ribbon-legend i { font-style:normal; display:inline-flex; align-items:center; gap:5px; }
  .ribbon-legend i::before { content:""; width:9px; height:9px; border-radius:2px; }
  .ribbon-legend .l-rec::before  { background: var(--warn); }
  .ribbon-legend .l-act::before  { background: var(--bad); }
  .ribbon-legend .l-hf::before   { background: var(--bad); box-shadow:0 0 5px var(--bad-dim); }
  .ribbon-legend .l-end::before  { background: var(--fg-faint); }

  /* --- host metric sparkline tiles --- */
  .hostgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(248px, 1fr));
              gap: 12px; margin-top: 12px; }
  .mc { background: var(--bg); border:1px solid var(--line); border-radius:3px; padding:9px 10px 6px; }
  .mc h3 { margin:0 0 2px; font-family:var(--mono); font-size:9.5px; color:var(--fg-dim);
           font-weight:600; text-transform:uppercase; letter-spacing:.07em;
           display:flex; justify-content:space-between; align-items:baseline; }
  .mc h3 b { font-size:13px; font-weight:700; color:var(--fg); }
  .mc h3 b.alert { color: var(--bad); }
  .mc canvas { height: 64px !important; }
  .verdict { margin-top:12px; font-family:var(--mono); font-size:11.5px; line-height:1.6;
             color: var(--warn); border-left:2px solid var(--warn-dim); padding-left:10px; }
  .verdict:empty { display:none; }

  /* --- host status bar (homescreen at-a-glance) --- */
  #statusbar { display:flex; align-items:stretch; margin-bottom:18px; border:1px solid var(--line);
               border-radius:4px; overflow:hidden; cursor:pointer; font-family:var(--mono); flex-wrap:wrap; }
  #statusbar .sx { padding:9px 16px; display:flex; flex-direction:column; gap:2px; justify-content:center;
                   min-width:104px; border-left:1px solid var(--line); }
  #statusbar .sx:first-child { border-left:none; }
  #statusbar .sx .l { font-size:9px; text-transform:uppercase; letter-spacing:.07em; color:var(--fg-faint); }
  #statusbar .sx .v { font-size:13px; font-weight:700; color:var(--fg); }
  #statusbar .sx .v.bad { color:var(--bad); } #statusbar .sx .v.warn { color:var(--warn); } #statusbar .sx .v.ok { color:var(--ok); }
  #statusbar .stat { min-width:148px; }
  #statusbar .stat .v { font-size:16px; letter-spacing:.12em; }
  #statusbar .stat.ok       { background:linear-gradient(180deg, rgba(63,185,80,.16), transparent); border-left:3px solid var(--ok); }
  #statusbar .stat.watch    { background:linear-gradient(180deg, rgba(224,168,46,.16), transparent); border-left:3px solid var(--warn); }
  #statusbar .stat.critical { background:linear-gradient(180deg, rgba(255,90,82,.20), transparent); border-left:3px solid var(--bad);
                              animation: pulse 1.6s ease-in-out infinite; }
  #statusbar .note { flex:1; min-width:240px; padding:9px 16px; font-size:10.5px; color:var(--warn);
                     display:flex; align-items:center; border-left:1px solid var(--line); }
  #statusbar .note:empty::after { content:"all clear"; color:var(--fg-faint); }

  /* --- error-spell list (inline-expandable rows) --- */
  .spelllist { margin-top:14px; display:flex; flex-direction:column; gap:6px; }
  .spell-item { border:1px solid var(--line); border-left-width:2px; border-radius:3px; overflow:hidden; background:var(--bg); }
  .spell-item.recovered { border-left-color:var(--warn); }
  .spell-item.active    { border-left-color:var(--bad); }
  .spell-item.hardfork  { border-left-color:var(--bad); box-shadow:inset 3px 0 14px var(--bad-dim); }
  .spell-item.ended     { border-left-color:var(--fg-faint); }
  .spell-item > summary { list-style:none; cursor:pointer; padding:7px 12px; display:flex; gap:12px; align-items:center;
                          font-family:var(--mono); font-size:11px; }
  .spell-item > summary::-webkit-details-marker { display:none; }
  .spell-item > summary::before { content:"\25B8"; color:var(--fg-dim); transition:transform .12s; flex:0 0 auto; }
  .spell-item[open] > summary::before { transform:rotate(90deg); }
  .spell-item[open] > summary { border-bottom:1px solid var(--line); background:var(--bg2); }
  .spell-item > summary:hover { background:var(--bg2); }
  .spell-item .st { flex:0 0 86px; font-weight:600; }
  .spell-item.recovered .st { color:var(--warn); } .spell-item.active .st { color:var(--bad); }
  .spell-item.hardfork .st { color:var(--bad); } .spell-item.ended .st { color:var(--fg-faint); }
  .spell-item .ins { flex:0 0 150px; color:var(--fg); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .spell-item .tg  { flex:0 0 124px; color:var(--bad); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .spell-item .ms  { flex:1 1 120px; color:var(--fg-dim); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .spell-item .tm  { flex:0 0 auto; color:var(--fg-faint); white-space:nowrap; }
  .spell-item > .sd { padding:14px; }
  .spelllist .empty-row { font-family:var(--mono); font-size:11px; color:var(--fg-faint); padding:10px; }

  /* --- shared spell-detail body (used inline in a row AND in the drawer) --- */
  .spell-detail h4 { font-family:var(--mono); font-size:10px; text-transform:uppercase; letter-spacing:.08em;
                     color:var(--fg-dim); margin:20px 0 8px; padding-bottom:5px; border-bottom:1px solid var(--line); }
  .spell-detail h4:first-child { margin-top:0; }
  .spell-detail .hdrline { font-family:var(--mono); font-size:11px; color:var(--fg-dim); margin-bottom:2px; line-height:1.6; }
  .spell-detail .hdrline b { color:var(--fg); } .spell-detail .hdrline b.bad { color:var(--bad); }
  .dgrid { display:grid; grid-template-columns:repeat(auto-fill, minmax(220px,1fr)); gap:10px; }
  .dgrid .mc canvas { height:56px !important; }
  .evtline { font-family:var(--mono); font-size:10.5px; max-height:200px; overflow-y:auto;
             border:1px solid var(--line); border-radius:3px; }
  .evtline .e { display:flex; gap:10px; padding:2px 8px; border-bottom:1px solid rgba(255,255,255,.03); }
  .evtline .e:last-child { border-bottom:none; }
  .evtline .e .t { color:var(--fg-faint); flex:0 0 64px; }
  .evtline .e .g { flex:0 0 110px; }
  .evtline .e.fork_warn .g, .evtline .e.error .g { color:var(--bad); }
  .evtline .e.consensus_lost .g, .evtline .e.out_of_sync .g { color:var(--warn); }
  .evtline .e.ledger_created .g { color:var(--ok); }
  .evtline .e .m { color:var(--fg-dim); white-space:pre-wrap; word-break:break-word; }
  /* artifact "pills" — nested expandable <details> inside a spell */
  .arts .cap-h { font-family:var(--mono); font-size:9.5px; color:var(--fg-dim); margin:12px 0 6px; display:flex; gap:8px; align-items:center; }
  .arts .cap-h:first-child { margin-top:0; }
  .arts .a { border:1px solid var(--line); border-radius:3px; margin-bottom:7px; overflow:hidden; }
  .arts .a > summary { cursor:pointer; padding:6px 10px; font-family:var(--mono); font-size:10.5px;
                       display:flex; gap:10px; align-items:center; list-style:none; background:var(--bg2); }
  .arts .a > summary::-webkit-details-marker { display:none; }
  .arts .a > summary::before { content:"\25B8"; color:var(--fg-dim); transition:transform .1s; }
  .arts .a[open] > summary::before { transform:rotate(90deg); }
  .arts .a > summary .k { color:var(--info); font-weight:600; }
  .arts .a > summary .when { color:var(--fg-faint); }
  .arts .a > summary .ins { color:var(--accent); }
  .arts .a > summary .sz { color:var(--fg-faint); }
  .arts .a > summary .grep { margin-left:auto; }
  .arts .a > summary .grep input { background:var(--bg); border:1px solid var(--line); color:var(--fg);
                                   font-family:var(--mono); font-size:10px; padding:1px 6px; width:130px; border-radius:2px; }
  .arts .a pre { margin:0; padding:10px 12px; background:var(--bg); font-family:var(--mono); font-size:10.5px;
                 line-height:1.5; overflow:auto; max-height:340px; white-space:pre-wrap; word-break:break-word; color:var(--fg); }
  .arts .a pre mark { background:rgba(224,168,46,.35); color:inherit; }
  .badge-cap { font-family:var(--mono); font-size:9px; color:var(--fg-dim);
               border:1px solid var(--line); border-radius:2px; padding:0 4px; }

  /* --- spell detail drawer (still used by per-instance card clicks) --- */
  #scrim { position:fixed; inset:0; background:rgba(0,0,0,.55); backdrop-filter:blur(2px);
           opacity:0; pointer-events:none; transition:opacity .18s; z-index:90; }
  #scrim.on { opacity:1; pointer-events:auto; }
  #drawer { position:fixed; top:0; right:0; bottom:0; width:min(880px,94vw); z-index:100;
            background: var(--bg1); border-left:1px solid var(--line2);
            box-shadow:-24px 0 60px rgba(0,0,0,.5); transform:translateX(100%);
            transition: transform .22s cubic-bezier(.4,0,.2,1); display:flex; flex-direction:column; }
  #drawer.on { transform:translateX(0); }
  #drawer .dh { padding:14px 18px; border-bottom:1px solid var(--line); display:flex; align-items:center; gap:12px; }
  #drawer .dh h2 { margin:0; font-family:var(--mono); font-size:14px; font-weight:600; }
  #drawer .dh .x { margin-left:auto; background:none; border:1px solid var(--line); color:var(--fg-dim);
                   width:26px; height:26px; border-radius:3px; cursor:pointer; font-size:14px; line-height:1; }
  #drawer .dh .x:hover { color:var(--fg); border-color:var(--line2); }
  #drawer .db { overflow-y:auto; padding:16px 18px 40px; flex:1; }
</style>
</head>
<body>
<header>
  <h1>sashi<span class="dot">.</span>mon</h1>
  <span class="meta">// hotpocket consensus &amp; host monitor</span>
  <span class="live" id="lastUpdate">connecting…</span>
</header>
<main>
  <div class="controls">
    <label>timeline
      <select id="window">
        <option value="900">15m</option>
        <option value="3600">1h</option>
        <option value="21600">6h</option>
        <option value="86400">24h</option>
        <option value="all" selected>all</option>
      </select>
    </label>
    <label>granularity
      <select id="bucket">
        <option value="30">30s</option>
        <option value="60" selected>1 min</option>
        <option value="300">5 min</option>
        <option value="3600">1 hour</option>
      </select>
    </label>
    <label>auto
      <select id="refresh">
        <option value="0">off</option>
        <option value="5000" selected>5s</option>
        <option value="15000">15s</option>
        <option value="60000">60s</option>
      </select>
    </label>
    <button id="reload">reload</button>
    <span style="flex:1"></span>
    <label class="policy-ctl" id="policyLabel" title="DB tracking policy. Balanced (default): low-impact spells (consensus_lost, out_of_sync, warning) skip metric-boost + snapshots, and during the spell their event flood is dropped. Forks still get full treatment. Full: track everything always. Minimal: only fork-class events.">tracking
      <select id="policy" disabled>
        <option value="balanced">balanced</option>
        <option value="full">full</option>
        <option value="minimal">minimal</option>
      </select>
    </label>
    <button id="export" title="download one plain-text report — every instance, every error spell, with the HotPocket log + journalctl + host metrics around each spell start; hand it to an analyst / LLM">export report</button>
    <button id="clearDb" class="danger" title="wipe ALL stored data: log events, host metrics, error spells & their artifacts, instances. Live tailing/sampling continues; history starts fresh.">clear dbs <span class="dbsz" id="dbSize">(—)</span></button>
  </div>

  <div id="clusterBanner" class="cluster-banner" style="display:none;">
    <span class="lbl">viewing cluster</span>
    <code class="cid" id="cbId"></code>
    <span id="cbMeta"></span>
    <button class="clr" id="cbClear">view all monitored ×</button>
  </div>

  <div class="panel" id="clusterPanel">
    <div class="ph">
      <h2>clusters</h2>
      <span class="hint" id="clusterHint">discovered Sashimono clusters on this VM. tick to monitor; click row to scope dashboard.</span>
      <span style="flex:1"></span>
      <button id="discoverBtn" title="run `sashi list` now to refresh the cluster list">discover</button>
    </div>
    <div class="pb" id="clusterList"></div>
  </div>

  <div id="statusbar" title="host status — click to jump to host metrics"></div>

  <div class="panel">
    <div class="ph">
      <h2>error events</h2>
      <span class="hint" id="globalHint">all instances · errors per minute · whole timeline</span>
    </div>
    <div class="pb">
      <div class="tagchips" id="tagChips"></div>
      <div class="global"><canvas id="globalChart"></canvas></div>
    </div>
  </div>

  <div class="panel">
    <div class="ph">
      <h2>host · this vm</h2>
      <span class="hint" id="hostMeta"></span>
    </div>
    <div class="pb">
      <div class="hostgrid">
        <div class="mc"><h3>disk free <span>MB · min mount</span><b id="v_disk">—</b></h3><canvas id="m_disk"></canvas></div>
        <div class="mc"><h3>mem used <span>%</span><b id="v_mem">—</b></h3><canvas id="m_mem"></canvas></div>
        <div class="mc"><h3>cpu / steal <span>%</span><b id="v_cpu">—</b></h3><canvas id="m_cpu"></canvas></div>
        <div class="mc"><h3>load <span>1m</span><b id="v_load">—</b></h3><canvas id="m_load"></canvas></div>
        <div class="mc"><h3>ntp offset <span>ms</span><b id="v_ntp">—</b></h3><canvas id="m_ntp"></canvas></div>
        <div class="mc"><h3>hp rss <span>MB · per inst</span><b id="v_rss">—</b></h3><canvas id="m_rss"></canvas></div>
      </div>
      <div class="verdict" id="hostVerdict"></div>
    </div>
  </div>

  <div class="panel">
    <div class="ph">
      <h2>error spells</h2>
      <span class="hint">consensus_loss / fork / out_of_sync · click a row to expand → host metrics &amp; journalctl/log artifacts for that spell</span>
    </div>
    <div class="pb">
      <div class="ribbon" id="ribbon"><div class="now"></div></div>
      <div class="ribbon-legend">
        <i class="l-rec">recovered (ledger resumed)</i>
        <i class="l-act">active now</i>
        <i class="l-hf">hard fork? (open &gt;120s, no ledger)</i>
        <i class="l-end">ended</i>
        <span style="margin-left:auto" id="spellCount"></span>
      </div>
      <div class="spelllist" id="spellRows"></div>
    </div>
  </div>

  <div id="cards" class="grid"></div>
</main>

<div id="scrim"></div>
<aside id="drawer">
  <div class="dh">
    <h2 id="dwTitle">spell</h2>
    <span class="pill" id="dwState">—</span>
    <button class="x" id="dwClose">&times;</button>
  </div>
  <div class="db" id="dwBody"></div>
</aside>
<script>
// All event tags, in render order. `err` marks the ones shown by default.
const TAGS = [
  { id:'fork_warn',        label:'fork',            color:'#f85149', err:true  },
  { id:'consensus_lost',   label:'consensus lost',  color:'#d29922', err:true  },
  { id:'out_of_sync',      label:'out of sync',     color:'#bc8cff', err:true  },
  { id:'error',            label:'error',           color:'#ff7b72', err:true  },
  { id:'warning',          label:'warning',         color:'#e3b341', err:false },
  { id:'ledger_created',   label:'ledger created',  color:'#3fb950', err:false },
  { id:'contract_running', label:'contract run',    color:'#1f6feb', err:false },
  { id:'hp_started',       label:'hp started',      color:'#79c0ff', err:false },
  { id:'hp_stopped',       label:'hp stopped',      color:'#8b949e', err:false },
  { id:'role_change',      label:'role change',     color:'#56d4dd', err:false },
  { id:'info_other',       label:'info / other',    color:'#484f58', err:false },
];
const TAG_BY_ID = Object.fromEntries(TAGS.map(t => [t.id, t]));
const TAG_COLORS = Object.fromEntries(TAGS.map(t => [t.id, t.color]));   // legacy refs (per-instance bars)
let VISIBLE_TAGS = new Set(TAGS.filter(t => t.err).map(t => t.id));      // default: error tags only

let charts = {};
let globalChart = null;
let timer = null;
let LAST_GLOBAL_BUCKETS = [];
let LAST_GLOBAL_BUCKETSEC = 60;
let LAST_INST_BUCKETS = {};

function fmtAge(s) {
  if (s == null) return '—';
  if (s < 60) return s.toFixed(0) + 's';
  if (s < 3600) return (s/60).toFixed(1) + 'm';
  return (s/3600).toFixed(1) + 'h';
}
function bucketLabel(sec) { return sec >= 86400 ? 'day' : sec >= 3600 ? 'hour' : sec >= 60 ? 'minute' : 'tick'; }
function hexA(hex, a) { const n = parseInt(hex.replace('#',''),16); return `rgba(${(n>>16)&255},${(n>>8)&255},${n&255},${a})`; }

async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + ' ' + r.status);
  return r.json();
}

// --- tag filter chips ---
function renderTagChips() {
  const host = document.getElementById('tagChips'); if (!host) return;
  let html = `<span class="lbl">show</span>`;
  for (const t of TAGS) {
    const on = VISIBLE_TAGS.has(t.id);
    html += `<span class="c${on?' on':''}" data-tag="${t.id}" style="--cc:${t.color}">${esc(t.label)}</span>`;
  }
  html += `<span style="flex:1"></span>` +
    `<span class="q danger" data-quick="err">errors only</span>` +
    `<span class="q" data-quick="all">all</span>` +
    `<span class="q" data-quick="none">none</span>`;
  host.innerHTML = html;
  host.querySelectorAll('.c[data-tag]').forEach(c => c.onclick = () => {
    const id = c.dataset.tag;
    if (VISIBLE_TAGS.has(id)) VISIBLE_TAGS.delete(id); else VISIBLE_TAGS.add(id);
    renderTagChips(); renderGlobalChart(); rerenderInstanceCharts();
  });
  host.querySelectorAll('.q[data-quick]').forEach(q => q.onclick = () => {
    const m = q.dataset.quick;
    VISIBLE_TAGS = m === 'all' ? new Set(TAGS.map(t=>t.id))
                 : m === 'none' ? new Set()
                 : new Set(TAGS.filter(t=>t.err).map(t=>t.id));
    renderTagChips(); renderGlobalChart(); rerenderInstanceCharts();
  });
}

// --- the main "error events" line chart ---
function lineDatasets(buckets, tagIds, fill) {
  return tagIds.filter(id => buckets.some(b => (b[id]||0) > 0)).map(id => {
    const t = TAG_BY_ID[id] || { color:'#888', label:id };
    return {
      label: t.label,
      data: buckets.map(b => ({ x: b.bucket_start*1000, y: b[id] || 0 })),
      borderColor: t.color, backgroundColor: fill ? hexA(t.color, 0.16) : 'transparent',
      fill: !!fill, tension: 0.3, borderWidth: 1.8, pointRadius: 0, pointHoverRadius: 3, spanGaps: true,
    };
  });
}
function renderGlobalChart() {
  const el = document.getElementById('globalChart'); if (!el) return;
  const buckets = LAST_GLOBAL_BUCKETS, sec = LAST_GLOBAL_BUCKETSEC;
  const vis = TAGS.filter(t => VISIBLE_TAGS.has(t.id)).map(t => t.id);
  const ds = lineDatasets(buckets, vis, true);
  const cfg = {
    type: 'line',
    data: { datasets: ds.length ? ds : [{ label:'(no matching events)', data:[], borderColor:'#3a4452' }] },
    options: {
      responsive:true, maintainAspectRatio:false, animation:false, parsing:false, normalized:true,
      interaction:{ mode:'index', intersect:false },
      plugins:{
        legend:{ display:true, position:'bottom',
          labels:{ color:'#8b949e', font:{family:"'IBM Plex Mono', monospace", size:10}, usePointStyle:true, boxWidth:8, boxHeight:8, padding:10 } },
        tooltip:{ titleFont:{family:"'IBM Plex Mono', monospace", size:10}, bodyFont:{family:"'IBM Plex Mono', monospace", size:10},
          callbacks:{ title:items => items.length ? new Date(items[0].parsed.x).toLocaleString() : '' } },
      },
      scales:{
        x:{ type:'time', time:{ unit: sec>=86400?'day':sec>=3600?'hour':'minute',
              displayFormats:{ minute:'HH:mm', hour:'MMM d HH:mm', day:'MMM d' } },
            ticks:{ color:'#6b7989', font:{family:"'IBM Plex Mono', monospace", size:9}, maxRotation:0, autoSkipPadding:18 },
            grid:{ display:false } },
        y:{ beginAtZero:true, ticks:{ color:'#6b7989', font:{family:"'IBM Plex Mono', monospace", size:9}, maxTicksLimit:5, precision:0 },
            grid:{ color:'rgba(255,255,255,.05)' } },
      },
    },
  };
  try {
    if (globalChart && globalChart.canvas !== el) { try{globalChart.destroy();}catch(e){} globalChart = null; }
    if (!globalChart) globalChart = new Chart(el, cfg);
    else { globalChart.data = cfg.data; globalChart.options = cfg.options; globalChart.update('none'); }
  } catch(e) {
    cfg.options.scales.x = { ticks:{display:false}, grid:{display:false} };
    cfg.data.datasets.forEach(d => d.data = d.data.map(p => p && p.y));
    cfg.data.labels = buckets.map(b => tsLabel(b.bucket_start));
    if (globalChart) { try{globalChart.destroy();}catch(_){} }
    globalChart = new Chart(el, cfg);
  }
}

// --- per-instance card mini-charts: small bars, same tag filter ---
function buildBarDatasets(buckets) {
  const vis = TAGS.filter(t => VISIBLE_TAGS.has(t.id)).map(t => t.id);
  const labels = buckets.map(b => tsLabel(b.bucket_start));
  const datasets = vis.filter(id => buckets.some(b => (b[id]||0) > 0)).map(id => ({
    label: (TAG_BY_ID[id]||{}).label || id, data: buckets.map(b => b[id] || 0),
    backgroundColor: (TAG_BY_ID[id]||{}).color || '#888', stack:'a',
  }));
  return { labels, datasets };
}
function barOpts() {
  return { responsive:true, maintainAspectRatio:false, animation:false,
    plugins:{ legend:{ display:false } },
    scales:{ x:{ stacked:true, ticks:{color:'#4a5765', font:{family:"'IBM Plex Mono', monospace", size:8}, maxTicksLimit:4}, grid:{display:false} },
             y:{ stacked:true, ticks:{color:'#4a5765', font:{family:"'IBM Plex Mono', monospace", size:8}, maxTicksLimit:3, precision:0}, grid:{color:'rgba(255,255,255,.04)'} } } };
}
function rerenderInstanceCharts() {
  for (const [name, buckets] of Object.entries(LAST_INST_BUCKETS)) {
    const cv = document.getElementById('ch-' + name); if (!cv) continue;
    const data = buildBarDatasets(buckets);
    if (!charts[name]) charts[name] = new Chart(cv, { type:'bar', data, options: barOpts() });
    else { charts[name].data = data; charts[name].update('none'); }
  }
  if (typeof updateGlobalHint === 'function') updateGlobalHint();
}

// ---- host metrics + error-spell console -----------------------------
const lineCharts = {};
function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function tsLabel(t){ return new Date(t*1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}); }
function tsClock(t){ return new Date(t*1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'}); }
function pick(rows, key){ return rows.map(r => (r[key]==null ? null : r[key])); }
function num(x, d){ return x==null ? '—' : (typeof x==='number' ? (Math.abs(x)>=100?Math.round(x):x.toFixed(d==null?1:d)) : x); }
function setVal(id, txt, alert){
  const el = document.getElementById(id); if(!el) return;
  el.textContent = txt; el.classList.toggle('alert', !!alert);
}

function lineChart(id, datasets, opts) {
  const el = document.getElementById(id); if (!el) return;
  opts = opts || {};
  const cfg = {
    type: 'line',
    data: { datasets: datasets.map(d => ({
      label: d.label, data: d.points, borderColor: d.color,
      backgroundColor: (d.fill ? d.color + '22' : 'transparent'), fill: !!d.fill,
      borderWidth: 1.4, tension: 0.18, pointRadius: 0, spanGaps: true,
    })) },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      parsing: false, normalized: true,
      plugins: { legend: { display: datasets.length > 1, position: 'bottom',
                   labels:{ color:'#6b7989', font:{family:"'IBM Plex Mono', monospace", size:9}, boxWidth:8, boxHeight:8, padding:6 } },
                 tooltip: { mode:'index', intersect:false,
                   titleFont:{family:"'IBM Plex Mono', monospace", size:10}, bodyFont:{family:"'IBM Plex Mono', monospace", size:10} } },
      scales: {
        x: { type:'time', time:{ unit:'minute', displayFormats:{minute:'HH:mm'} },
             ticks:{ color:'#4a5765', font:{family:"'IBM Plex Mono', monospace", size:8}, maxTicksLimit:4, maxRotation:0 },
             grid:{ color:'rgba(255,255,255,.03)' } },
        y: { ticks:{ color:'#4a5765', font:{family:"'IBM Plex Mono', monospace", size:8}, maxTicksLimit:4 },
             grid:{ color:'rgba(255,255,255,.05)' }, beginAtZero: !opts.noZero,
             suggestedMin: opts.suggestedMin, suggestedMax: opts.suggestedMax },
      },
    },
  };
  // If a prior chart bound a now-stale canvas (drawer reopened / row re-rendered), drop it.
  if (lineCharts[id] && lineCharts[id].canvas !== el) {
    try { lineCharts[id].destroy(); } catch(e) {}
    delete lineCharts[id];
  }
  // Chart.js time scale needs adapters; fall back to category if 'time' fails.
  try {
    if (!lineCharts[id]) lineCharts[id] = new Chart(el, cfg);
    else { lineCharts[id].data = cfg.data; lineCharts[id].options = cfg.options; lineCharts[id].update('none'); }
  } catch(e) {
    // No time adapter loaded — degrade to a plain index axis.
    cfg.options.scales.x = { ticks:{display:false}, grid:{display:false} };
    cfg.data.datasets.forEach(d => d.data = d.data.map(p => p && p.y));
    if (lineCharts[id]) { try { lineCharts[id].destroy(); } catch(_) {} }
    lineCharts[id] = new Chart(el, cfg);
  }
}
function destroyCharts(pred) {
  for (const k of Object.keys(lineCharts)) if (pred(k)) { try { lineCharts[k].destroy(); } catch(e) {} delete lineCharts[k]; }
}
const xy = (rows, key) => rows.filter(r => r[key] != null).map(r => ({ x: r.ts*1000, y: r[key] }));
const PAL = ['#3fb950','#58a6ff','#e0a82e','#b083ff','#ff5a52','#56d4dd','#d2a8ff'];

let LAST_SPELLS = [];        // all spells (24h), cached for ribbon + per-card lists
let LAST_PROC_LATEST = {};   // instance -> most recent per-process host_metrics row
let CARD_SPELLS_ALL = {};    // instance -> bool: card showing all its spells vs first few
let OPEN_SPELLS = new Set(); // spell_ids currently expanded in the spells panel (kept across refreshes)
let SPELL_LIST_KEY = '';     // signature of the rendered spell list, to avoid needless rebuilds

async function refreshHost(win) {
  let machine = [], procRows = [], spells = [];
  try {
    [machine, procRows, spells] = await Promise.all([
      fetchJSON(`/api/host_metrics?instance=machine&window=${win}`),
      fetchJSON(`/api/host_metrics?window=${win}&limit=20000`),
      fetchJSON(`/api/spells_log?window=86400` + clusterQS()),
    ]);
  } catch (e) { document.getElementById('hostMeta').textContent = 'host: ' + e.message; return; }
  machine = machine.slice().reverse();                 // API returns DESC
  const last = machine[machine.length - 1] || null;

  // ---- headline values + sparkline tiles ----
  if (last) {
    let dl = '';
    try { dl = Object.entries(JSON.parse(last.extra||'{}').disks||{})
      .map(([m,d]) => `${m}:${d.used_pct}%`).join(' '); } catch(e){}
    document.getElementById('hostMeta').textContent =
      `${fmtAge((Date.now()/1000)-last.ts)} ago · ${machine.length} samples` + (dl ? ` · ${dl}` : '') +
      (last.ntp_synced===0 ? ' · NTP NOT SYNCED' : '');
    setVal('v_disk', num(last.disk_free_mb,0), last.disk_free_mb!=null && last.disk_free_mb < 1024);
    setVal('v_mem',  num(last.mem_used_pct), last.mem_used_pct!=null && last.mem_used_pct > 92);
    setVal('v_cpu',  `${num(last.cpu_pct)}/${num(last.steal_pct)}`, (last.steal_pct||0) > 10);
    setVal('v_load', num(last.load1), false);
    setVal('v_ntp',  num(last.ntp_offset_ms), last.ntp_synced===0 || Math.abs(last.ntp_offset_ms||0) > 1000);
  } else {
    document.getElementById('hostMeta').textContent = 'no host samples yet (metrics disabled?)';
  }
  lineChart('m_disk', [{label:'free MB', color:PAL[0], points: xy(machine,'disk_free_mb'), fill:true}]);
  lineChart('m_mem',  [{label:'mem %', color:PAL[2], points: xy(machine,'mem_used_pct')},
                       {label:'swap MB', color:PAL[3], points: xy(machine,'swap_used_mb')}], {suggestedMax:100});
  lineChart('m_cpu',  [{label:'cpu %', color:PAL[1], points: xy(machine,'cpu_pct')},
                       {label:'steal %', color:PAL[4], points: xy(machine,'steal_pct')}], {suggestedMax:100});
  lineChart('m_load', [{label:'load1', color:PAL[5], points: xy(machine,'load1'), fill:true}]);
  lineChart('m_ntp',  [{label:'ntp ms', color:PAL[1], points: xy(machine,'ntp_offset_ms')}], {noZero:true});

  // per-instance HP-process RSS (procRows arrives ts-DESC → first per inst = latest)
  LAST_PROC_LATEST = {};
  for (const r of procRows) if (r.instance && !(r.instance in LAST_PROC_LATEST)) LAST_PROC_LATEST[r.instance] = r;
  const byInst = {};
  for (const r of procRows) if (r.instance && r.proc_rss_mb != null) (byInst[r.instance] ||= []).push(r);
  const rssDs = Object.entries(byInst).map(([name, rows], i) => {
    rows.sort((a,b)=>a.ts-b.ts);
    return { label: name.slice(0,12), color: PAL[i % PAL.length], points: rows.map(r=>({x:r.ts*1000, y:r.proc_rss_mb})) };
  });
  lineChart('m_rss', rssDs.length ? rssDs : [{label:'rss', color:'#3a4452', points:[]}]);
  const lastRss = rssDs.length ? rssDs.map(d => d.points.length ? Math.round(d.points[d.points.length-1].y) : null).filter(x=>x!=null) : [];
  setVal('v_rss', lastRss.length ? lastRss.join('/') : '—',
         lastRss.some(x => x > 700));

  // ---- verdict heuristic ----
  const v = [];
  if (last) {
    if (last.disk_free_mb!=null && last.disk_free_mb < 1024) v.push(`disk min-free ${Math.round(last.disk_free_mb)}MB — near full → ledger persistence fails (set history=custom / bigger disk)`);
    if (last.inode_used_pct!=null && last.inode_used_pct > 85) v.push(`inodes ${last.inode_used_pct}% used`);
    if ((last.steal_pct||0) > 10) v.push(`CPU steal ${last.steal_pct}% — burstable/noisy VM → missed round windows`);
    if (last.ntp_synced===0 || Math.abs(last.ntp_offset_ms||0) > 1000) v.push(`clock ${last.ntp_synced===0?'NOT SYNCED':('off '+Math.round(last.ntp_offset_ms||0)+'ms')} — round-boundary desync risk (run chrony)`);
    if (last.mem_avail_mb!=null && last.mem_avail_mb < 256) v.push(`only ${Math.round(last.mem_avail_mb)}MB free — OOM risk → node restart = a fault`);
  }
  for (const [name, rows] of Object.entries(byInst)) {
    if (rows.length >= 5) { const a=rows[0].proc_rss_mb, b=rows[rows.length-1].proc_rss_mb;
      if (a>0 && b>a*2 && b-a>100) v.push(`${name.slice(0,16)} RSS ${Math.round(a)}→${Math.round(b)}MB this window — possible leak → eventual OOM`); }
  }
  const openHF = spells.filter(s => s.state === 'hard_fork?' || s.state === 'active');
  if (openHF.length) v.unshift(`${openHF.length} spell(s) OPEN now — ${openHF.map(s=>(s.instance||'').slice(0,12)).join(', ')} (4-node clusters tolerate only 1 fault — see HOTPOCKET_CONSENSUS_INVESTIGATION.md)`);
  document.getElementById('hostVerdict').innerHTML = v.length ? v.map(t=>'⚠ '+esc(t)).join('<br>') : '';

  // ---- status bar + spell ribbon + inline-expandable spell list ----
  LAST_SPELLS = spells;
  const lastAge = last ? (Date.now()/1000 - last.ts) : null;
  renderStatusBar(last, v, openHF, lastAge, byInst);
  renderRibbon(spells);
  renderSpellsPanel(spells);
}

// ---- host status bar (homescreen at-a-glance) -----------------------
function renderStatusBar(m, verdictLines, openSpells, lastAge, byInst) {
  const sb = document.getElementById('statusbar'); if (!sb) return;
  const open = (openSpells || []).length;
  let level, word;
  if (open) { level = 'critical'; word = open + ' SPELL' + (open > 1 ? 'S' : '') + ' OPEN'; }
  else if ((verdictLines || []).length) { level = 'watch'; word = 'WATCH'; }
  else if (!m) { level = 'watch'; word = 'NO HOST DATA'; }
  else { level = 'ok'; word = 'OK'; }
  const cell = (l, val, cls) => `<div class="sx"><span class="l">${l}</span><span class="v ${cls || ''}">${val}</span></div>`;
  const ninst = byInst ? Object.keys(byInst).length : 0;
  let html = `<div class="sx stat ${level}"><span class="l">cluster · host</span><span class="v">${esc(word)}</span></div>`;
  if (m) {
    html += cell('disk free', m.disk_free_mb != null ? Math.round(m.disk_free_mb) + 'M' : '—', (m.disk_free_mb != null && m.disk_free_mb < 1024) ? 'bad' : '');
    html += cell('mem used', m.mem_used_pct != null ? m.mem_used_pct + '%' : '—', (m.mem_used_pct || 0) > 92 ? 'bad' : '');
    html += cell('cpu/steal', `${num(m.cpu_pct)}/${num(m.steal_pct)}`, (m.steal_pct || 0) > 10 ? 'warn' : '');
    html += cell('load1', num(m.load1), (m.load1 || 0) > 8 ? 'warn' : '');
    html += cell('ntp', m.ntp_synced === 0 ? 'NOSYNC' : (m.ntp_offset_ms != null ? num(m.ntp_offset_ms) + 'ms' : '—'), (m.ntp_synced === 0 || Math.abs(m.ntp_offset_ms || 0) > 1000) ? 'bad' : '');
    html += cell('hp inst', String(ninst));
    html += cell('sampled', lastAge != null ? fmtAge(lastAge) + ' ago' : '—', (lastAge != null && lastAge > 120) ? 'warn' : '');
  }
  html += cell('spells 24h', String((LAST_SPELLS || []).length), open ? 'bad' : '');
  const notes = (verdictLines || []).slice();
  html += `<div class="note">${notes.slice(0, 2).map(esc).join('  ·  ')}${notes.length > 2 ? '  ·  +' + (notes.length - 2) + ' more' : ''}</div>`;
  sb.className = level === 'critical' ? 'critical' : '';
  sb.innerHTML = html;
  sb.onclick = () => { const el = document.getElementById('hostVerdict') || document.getElementById('m_disk'); if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' }); };
}

// ---- 24h spell timeline ribbon --------------------------------------
function renderRibbon(spells) {
  const rb = document.getElementById('ribbon');
  rb.querySelectorAll('.blk, .axl').forEach(e => e.remove());
  const now = Date.now() / 1000, span = 86400, t0 = now - span;
  for (let h = 0; h <= 24; h += 6) {
    const ax = document.createElement('div'); ax.className = 'axl';
    ax.style.left = (100 * h / 24) + '%';
    ax.textContent = h === 0 ? '-24h' : (h === 24 ? 'now' : '-' + (24 - h) + 'h');
    rb.appendChild(ax);
  }
  for (const s of spells) {
    const a = Math.max(s.start_ts, t0), b = Math.min(s.end_ts || now, now);
    if (b <= t0) continue;
    const blk = document.createElement('div');
    blk.className = 'blk ' + spellCls(s);
    blk.style.left = (100 * (a - t0) / span) + '%';
    blk.style.width = Math.max(0.25, 100 * (b - a) / span) + '%';
    blk.title = `${s.instance} · ${s.state} · ${tsClock(s.start_ts)} · ${fmtAge(s.duration_s)} · ${s.trigger_tag || ''}`;
    blk.onclick = () => expandSpellRow(s.spell_id);
    rb.appendChild(blk);
  }
}

function expandSpellRow(spellId) {
  const it = document.querySelector(`.spell-item[data-sid="${CSS.escape(spellId)}"]`);
  if (!it) { openSpellDrawer(spellId); return; }   // not in the panel list (e.g. clicked from a card) → drawer
  if (!it.open) it.open = true;                     // toggle listener lazy-loads the body
  it.scrollIntoView({ behavior: 'smooth', block: 'center' });
  it.style.outline = '1px solid var(--accent)';
  setTimeout(() => { it.style.outline = ''; }, 1400);
}

// ---- inline-expandable spell list (spells panel) --------------------
const spellCls = s => s.state === 'recovered' ? 'recovered' : s.state === 'active' ? 'active' : s.state === 'hard_fork?' ? 'hardfork' : 'ended';
const durBucket = s => String(Math.round((s.duration_s || 0) / 30));
const spellPrefix = id => 'r' + String(id).replace(/[^a-zA-Z0-9]/g, '_');
function spellSummaryHTML(s) {
  return `<span class="st">${esc(s.state)}</span>` +
    `<span class="ins">${esc((s.instance || '').slice(0, 22))}</span>` +
    `<span class="tg">${esc(s.trigger_tag || '')}</span>` +
    `<span class="ms">${esc((s.trigger_msg || '').slice(0, 220))}</span>` +
    `<span class="tm">${tsClock(s.start_ts)} · ${fmtAge(s.duration_s)} · cap ${s.captures || 0}</span>`;
}

function renderSpellsPanel(spells) {
  const host = document.getElementById('spellRows');
  document.getElementById('spellCount').textContent = spells.length
    ? `${spells.length} in 24h · ${spells.filter(s => s.state !== 'recovered' && s.state !== 'ended').length} unresolved` : '';
  // only rebuild when something an expanded row would care about changed
  const key = spells.map(s => `${s.spell_id}|${s.state}|${durBucket(s)}|${s.captures || 0}`).join('~');
  if (key === SPELL_LIST_KEY && host.childElementCount) return;   // unchanged → keep the DOM (and any expanded rows)
  SPELL_LIST_KEY = key;
  destroyCharts(k => k[0] === 'r');                 // tear down old inline-row charts before rebuilding
  host.innerHTML = '';
  if (!spells.length) { host.innerHTML = '<div class="empty-row">— no error spells in the last 24h —</div>'; return; }
  for (const s of spells) {
    const det = document.createElement('details');
    det.className = 'spell-item ' + spellCls(s);
    det.dataset.sid = s.spell_id;
    det.innerHTML = `<summary>${spellSummaryHTML(s)}</summary>` +
      `<div class="sd"><div style="color:var(--fg-dim);font-family:var(--mono);font-size:11px">expand to load host metrics &amp; journalctl/log artifacts…</div></div>`;
    const sd = det.querySelector('.sd');
    const loadDetail = () => {
      const want = durBucket(s);
      if (det.dataset.loadedDur === want) return;   // body already current
      det.dataset.loadedDur = want;
      renderSpellDetail(sd, s.spell_id, spellPrefix(s.spell_id));
    };
    det.addEventListener('toggle', () => {
      if (det.open) { OPEN_SPELLS.add(s.spell_id); loadDetail(); }
      else { OPEN_SPELLS.delete(s.spell_id); }
    });
    host.appendChild(det);
    if (OPEN_SPELLS.has(s.spell_id)) { det.open = true; loadDetail(); }   // restore expansion across refreshes
  }
}

// ---- shared spell-detail renderer (used by inline rows AND the drawer) ----
const ARTIFACT_ORDER = ['logtail', 'journalctl', 'journalctl_agent', 'journalctl_units', 'dmesg', 'ps', 'free', 'vmstat', 'uptime', 'df', 'dfi', 'chronyc', 'du'];

async function renderSpellDetail(container, spellId, prefix) {
  const meta = LAST_SPELLS.find(s => s.spell_id === spellId) || {};
  container.innerHTML = '<div style="color:var(--fg-dim);font-family:var(--mono);font-size:11px">loading…</div>';
  const startTs = meta.start_ts || (Date.now() / 1000 - 600);
  const endTs = meta.end_ts || (Date.now() / 1000);
  const p0 = startTs - 90, p1 = endTs + 90;
  let arts = [], hm = [], evs = [];
  try {
    [arts, hm, evs] = await Promise.all([
      fetchJSON('/api/spell_artifacts?spell_id=' + encodeURIComponent(spellId)),
      fetchJSON(`/api/host_metrics?window=86400&limit=20000`),
      fetchJSON(`/api/events?instance=${encodeURIComponent(meta.instance || '')}&since=${p0}&until=${p1}&limit=2000`),
    ]);
  } catch (e) { container.innerHTML = '<div style="color:var(--bad)">error: ' + esc(e.message) + '</div>'; return; }

  const win = hm.filter(r => r.ts >= p0 && r.ts <= p1);
  const mach = win.filter(r => r.instance == null).sort((a, b) => a.ts - b.ts);
  const procByInst = {};
  for (const r of win) if (r.instance && r.proc_rss_mb != null) (procByInst[r.instance] ||= []).push(r);
  const boosted = win.filter(r => r.during_spell === 1).length;
  const dur = meta.duration_s != null ? fmtAge(meta.duration_s) : '—';
  const cid = k => `${prefix}_${k}`;

  let html = `<div class="spell-detail">`;
  html += `<div class="hdrline">id <b>${esc(spellId)}</b> · instance <b>${esc(meta.instance || '?')}</b> · ` +
    `state <b class="${(meta.state && meta.state !== 'recovered' && meta.state !== 'ended') ? 'bad' : ''}">${esc(meta.state || '?')}</b> · ` +
    `started <b>${esc(new Date(startTs * 1000).toLocaleString())}</b> · duration <b>${dur}</b> · ` +
    `trigger <b class="bad">${esc(meta.trigger_tag || '?')}</b> ${esc((meta.trigger_msg || '').slice(0, 160))}</div>` +
    `<div class="hdrline">${arts.length} artifacts · ${boosted} boosted (3s) host samples · ${evs.length} log events in window</div>`;

  html += `<h4>host metrics — spell window ±90s${boosted ? ` · ${boosted} boosted samples` : ''}</h4>`;
  html += `<div class="dgrid">` +
    `<div class="mc"><h3>disk free <span>MB</span></h3><canvas id="${cid('disk')}"></canvas></div>` +
    `<div class="mc"><h3>mem used <span>%</span></h3><canvas id="${cid('mem')}"></canvas></div>` +
    `<div class="mc"><h3>cpu / steal <span>%</span></h3><canvas id="${cid('cpu')}"></canvas></div>` +
    `<div class="mc"><h3>load <span>1m</span></h3><canvas id="${cid('load')}"></canvas></div>` +
    `<div class="mc"><h3>ntp offset <span>ms</span></h3><canvas id="${cid('ntp')}"></canvas></div>` +
    `<div class="mc"><h3>hp rss <span>MB</span></h3><canvas id="${cid('rss')}"></canvas></div></div>`;

  html += `<h4>events around the spell (${evs.length})</h4>`;
  if (!evs.length) html += `<div style="color:var(--fg-faint);font-family:var(--mono);font-size:11px">no log events captured for this instance in the window</div>`;
  else {
    const ord = evs.slice().sort((a, b) => a.ts - b.ts);
    html += `<div class="evtline">` + ord.map(e =>
      `<div class="e ${esc(e.tag)}"><span class="t">${tsClock(e.ts)}</span><span class="g">${esc(e.tag)}</span><span class="m">${esc((e.msg || '').slice(0, 400))}</span></div>`
    ).join('') + `</div>`;
  }

  html += `<h4>captured snapshots — journalctl / dmesg / ps / df / chronyc / contract log (${arts.length}) — each is its own expandable pill with a grep box</h4>`;
  if (!arts.length) html += `<div style="color:var(--fg-faint);font-family:var(--mono);font-size:11px">no artifacts yet — spell may be new, or capture tools (ps/df/journalctl) unavailable on this host</div>`;
  else {
    const sorted = arts.slice().sort((a, b) => (a.ts - b.ts) || (ARTIFACT_ORDER.indexOf(a.kind) - ARTIFACT_ORDER.indexOf(b.kind)));
    const groups = [];
    for (const a of sorted) { let g = groups.find(g => Math.abs(g.ts - a.ts) < 5); if (!g) { g = { ts: a.ts, items: [] }; groups.push(g); } g.items.push(a); }
    html += `<div class="arts">` + groups.map((g, gi) =>
      `<div class="cap-h"><span class="badge-cap">capture ${gi + 1}/${groups.length}</span> ${esc(new Date(g.ts * 1000).toLocaleString())}</div>` +
      g.items.map((a, ai) => {
        const id = `${prefix}_art_${gi}_${ai}`;
        const sz = (a.content || '').length;
        const openDef = ['logtail', 'journalctl_agent', 'journalctl', 'dmesg'].includes(a.kind) && gi === 0;
        return `<details class="a"${openDef ? ' open' : ''}><summary>` +
          `<span class="k">${esc(a.kind)}</span>` + (a.instance ? `<span class="ins">${esc(a.instance.slice(0, 18))}</span>` : '') +
          `<span class="when">${tsClock(a.ts)}</span><span class="sz">${sz > 2048 ? Math.round(sz / 1024) + 'K' : sz + 'B'}</span>` +
          `<span class="grep"><input placeholder="grep…" data-tgt="${id}" oninput="grepArtifact(this)"></span></summary>` +
          `<pre id="${id}" data-raw="${esc(a.content || '')}">${esc(a.content || '(empty)')}</pre></details>`;
      }).join('')
    ).join('') + `</div>`;
  }
  html += `</div>`;
  container.innerHTML = html;

  lineChart(cid('disk'), [{ label: 'free MB', color: PAL[0], points: xy(mach, 'disk_free_mb'), fill: true }]);
  lineChart(cid('mem'), [{ label: 'mem %', color: PAL[2], points: xy(mach, 'mem_used_pct') }, { label: 'swap MB', color: PAL[3], points: xy(mach, 'swap_used_mb') }], { suggestedMax: 100 });
  lineChart(cid('cpu'), [{ label: 'cpu %', color: PAL[1], points: xy(mach, 'cpu_pct') }, { label: 'steal %', color: PAL[4], points: xy(mach, 'steal_pct') }], { suggestedMax: 100 });
  lineChart(cid('load'), [{ label: 'load1', color: PAL[5], points: xy(mach, 'load1'), fill: true }]);
  lineChart(cid('ntp'), [{ label: 'ntp ms', color: PAL[1], points: xy(mach, 'ntp_offset_ms') }], { noZero: true });
  const dwRss = Object.entries(procByInst).map(([n, rows], i) => { rows.sort((a, b) => a.ts - b.ts);
    return { label: n.slice(0, 12), color: PAL[i % PAL.length], points: rows.map(r => ({ x: r.ts * 1000, y: r.proc_rss_mb })) }; });
  lineChart(cid('rss'), dwRss.length ? dwRss : [{ label: 'rss', color: '#3a4452', points: [] }]);
}

// ---- spell detail drawer (kept for per-instance card clicks) --------
function closeDrawer(){ document.getElementById('drawer').classList.remove('on'); document.getElementById('scrim').classList.remove('on'); }
document.getElementById('dwClose').onclick = closeDrawer;
document.getElementById('scrim').onclick = closeDrawer;
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });

async function openSpellDrawer(spellId) {
  const meta = LAST_SPELLS.find(s => s.spell_id === spellId) || {};
  document.getElementById('dwTitle').textContent = spellId;
  const stEl = document.getElementById('dwState');
  stEl.className = 'pill ' + (meta.state === 'recovered' ? 'ok' : meta.state === 'ended' ? 'dim' : 'bad');
  stEl.textContent = meta.state || '—';
  document.getElementById('drawer').classList.add('on');
  document.getElementById('scrim').classList.add('on');
  await renderSpellDetail(document.getElementById('dwBody'), spellId, 'dw');
}

function grepArtifact(inp) {
  const pre = document.getElementById(inp.dataset.tgt); if (!pre) return;
  const raw = pre.dataset.raw || '';
  const q = inp.value.trim();
  if (!q) { pre.innerHTML = esc(raw); return; }
  const lit = q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');   // treat the query as a literal substring
  let rx; try { rx = new RegExp(lit, 'i'); } catch(e) { return; }
  const lines = raw.split('\n').filter(l => rx.test(l));
  const hrx = new RegExp(lit, 'ig');
  pre.innerHTML = (lines.length ? lines : ['(no match)']).map(l =>
    esc(l).replace(hrx, m => '<mark>' + esc(m) + '</mark>')
  ).join('\n');
}

// ---- per-instance card: current metrics + clickable spell list ------
function renderCardMetrics(card, name) {
  const el = card.querySelector('[data-pmet]'); if (!el) return;
  const m = LAST_PROC_LATEST[name];
  if (!m) {
    el.innerHTML = `<span class="lbl">host metrics</span><b>—</b>` +
      `<span style="color:var(--fg-faint)">(hp process not found, or --no-metrics)</span>`;
    return;
  }
  const age = fmtAge((Date.now()/1000) - m.ts);
  const rssAlert = m.proc_rss_mb != null && m.proc_rss_mb > 700;
  el.innerHTML =
    `<span class="lbl">rss</span><b class="${rssAlert?'alert':''}">${m.proc_rss_mb!=null?Math.round(m.proc_rss_mb)+'MB':'—'}</b>` +
    `<span class="lbl">open fds</span><b>${m.proc_open_fds!=null?m.proc_open_fds:'—'}</b>` +
    `<span class="lbl">pid</span><b>${m.proc_pid!=null?m.proc_pid:'—'}</b>` +
    `<span class="lbl">sampled</span><b>${age} ago</b>`;
}

function renderCardSpells(card, name) {
  const el = card.querySelector('[data-spells]'); if (!el) return;
  const mine = LAST_SPELLS.filter(s => s.instance === name).sort((a,b)=>b.start_ts-a.start_ts);
  if (!mine.length) {
    el.innerHTML = `<div class="hdr">error spells · 24h</div><div class="sp noclick">— clean —</div>`;
    return;
  }
  const showAll = !!CARD_SPELLS_ALL[name];
  const list = showAll ? mine : mine.slice(0, 5);
  const unresolved = mine.filter(s => s.state === 'active' || s.state === 'hard_fork?').length;
  let html = `<div class="hdr">error spells · 24h — ${mine.length}${unresolved ? ` · <b style="color:var(--bad)">${unresolved} open</b>` : ''} · click → expand</div>`;
  html += list.map(s => {
    const cls = s.state==='recovered'?'recovered' : s.state==='active'?'active' : s.state==='hard_fork?'?'hardfork' : 'ended';
    return `<div class="sp ${cls}" data-sid="${esc(s.spell_id)}" title="${esc(s.state)} · ${esc(s.trigger_tag||'')} · ${esc(new Date(s.start_ts*1000).toLocaleString())}">` +
      `<span class="st">${esc(s.state)}</span>` +
      `<span class="tg">${esc(s.trigger_tag||'')}${s.trigger_msg?' · '+esc((s.trigger_msg||'').slice(0,38)):''}</span>` +
      `<span class="tm">${tsClock(s.start_ts)} · ${fmtAge(s.duration_s)} · cap ${s.captures||0}</span></div>`;
  }).join('');
  if (mine.length > 5) html += `<div class="more" data-toggle="1">${showAll ? '▴ fewer' : '▾ +' + (mine.length - 5) + ' more'}</div>`;
  el.innerHTML = html;
  el.querySelectorAll('.sp[data-sid]').forEach(d => d.onclick = () => expandSpellRow(d.dataset.sid));
  const more = el.querySelector('.more[data-toggle]');
  if (more) more.onclick = () => { CARD_SPELLS_ALL[name] = !CARD_SPELLS_ALL[name]; renderCardSpells(card, name); };
}

function windowParam() {
  const raw = document.getElementById('window').value;
  return raw === 'all' ? 'all' : (+raw || 3600);
}
function updateGlobalHint() {
  const w = document.getElementById('window');
  const wt = (w.value === 'all') ? 'whole timeline' : (w.selectedOptions[0] ? w.selectedOptions[0].text : w.value);
  const onlyErr = VISIBLE_TAGS.size === TAGS.filter(t=>t.err).length && [...VISIBLE_TAGS].every(id => (TAG_BY_ID[id]||{}).err);
  const what = VISIBLE_TAGS.size === 0 ? 'nothing selected' : onlyErr ? 'errors' : (VISIBLE_TAGS.size === TAGS.length ? 'all tags' : VISIBLE_TAGS.size + ' tags');
  document.getElementById('globalHint').textContent = `all instances · ${what} per ${bucketLabel(LAST_GLOBAL_BUCKETSEC)} · ${wt}`;
}

async function refresh() {
  const wp = windowParam();
  const bucketSec = +document.getElementById('bucket').value || 60;

  const [summary, globalBuckets] = await Promise.all([
    fetchJSON('/api/summary?window=' + wp + clusterQS()),
    fetchJSON(`/api/histogram?window=${wp}&bucket=${bucketSec}` + clusterQS()),
  ]);
  // populate the host panel + LAST_SPELLS / LAST_PROC_LATEST before rendering cards
  try { await refreshHost(wp); }
  catch (e) { document.getElementById('hostMeta').textContent = 'host: ' + e.message; }

  // Main "error events" line chart (default tags = errors; toggle via chips)
  LAST_GLOBAL_BUCKETS = globalBuckets; LAST_GLOBAL_BUCKETSEC = bucketSec;
  renderTagChips();
  renderGlobalChart();
  updateGlobalHint();

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
        <div class="pmet" data-pmet></div>
        <canvas id="ch-${inst.name}"></canvas>
        <div class="cspells" data-spells></div>`;
      cards.appendChild(card);
    }
    card.querySelector('[data-sub]').innerHTML =
      `<span class="health h-${inst.health}">${inst.health}</span> · sashi: ${inst.sashi_status || '—'}`;
    const c = inst.counts || {};
    card.querySelector('[data-stats]').innerHTML = `
      <span>Last ledger</span><span>${fmtAge(inst.last_ledger_age_s)} ago</span>
      <span>Last event</span><span>${fmtAge(inst.last_event_age_s)} ago</span>
      <span>Ledgers</span><span>${c.ledger_created || 0}</span>
      <span>Consensus loss</span><span>${c.consensus_lost || 0}</span>
      <span>Fork warnings</span><span>${c.fork_warn || 0}</span>
      <span>Errors</span><span>${c.error || 0}</span>
      <span>Uptime est.</span><span>${inst.uptime_pct}%</span>`;
    renderCardMetrics(card, inst.name);
    renderCardSpells(card, inst.name);

    const buckets = await fetchJSON(
      `/api/histogram?window=${wp}&bucket=${bucketSec}&instance=${encodeURIComponent(inst.name)}` + clusterQS());
    LAST_INST_BUCKETS[inst.name] = buckets;
    const data = buildBarDatasets(buckets);
    if (!charts[inst.name]) charts[inst.name] = new Chart(document.getElementById('ch-' + inst.name), { type:'bar', data, options: barOpts() });
    else { charts[inst.name].data = data; charts[inst.name].update('none'); }
  }
  // Drop cards / charts for vanished instances
  for (const k of Object.keys(charts)) {
    if (!seen.has(k)) {
      const el = document.getElementById('c-' + k);
      if (el) el.remove();
      try { charts[k].destroy(); } catch(e) {}
      delete charts[k]; delete LAST_INST_BUCKETS[k];
    }
  }
  document.getElementById('lastUpdate').textContent = 'updated ' + new Date().toLocaleTimeString();
}

function scheduleRefresh() {
  if (timer) clearInterval(timer);
  const ms = +document.getElementById('refresh').value;
  if (ms > 0) timer = setInterval(refresh, ms);
}

// ---- cluster picker + filter ----------------------------------------
let ACTIVE_CLUSTER = (function(){
  try { const v = localStorage.getItem('sashimon.cluster'); return (v && v !== '__all__') ? v : null; }
  catch { return null; }
})();
let CLUSTERS = [];

function clusterQS() { return ACTIVE_CLUSTER ? ('&contract_id=' + encodeURIComponent(ACTIVE_CLUSTER)) : ''; }

function shortCid(id) {
  if (!id) return '—';
  if (id === '_unknown') return '(no contract_id)';
  return id.length > 16 ? id.slice(0,8) + '…' + id.slice(-4) : id;
}
function shortHash(s) {
  if (!s) return '—';
  return s.length > 14 ? s.slice(0,6) + '…' + s.slice(-4) : s;
}
function shortImage(s) {
  if (!s) return '';
  return s.replace(/^evernode(?:dev)?\//, '');
}

function setActiveCluster(cid) {
  ACTIVE_CLUSTER = cid || null;
  try { localStorage.setItem('sashimon.cluster', ACTIVE_CLUSTER || ''); } catch {}
  updateClusterBanner();
  renderClusters();
  hardReload();
}

function updateClusterBanner() {
  const banner = document.getElementById('clusterBanner');
  if (!banner) return;
  if (!ACTIVE_CLUSTER) { banner.style.display = 'none'; return; }
  const c = CLUSTERS.find(x => x.contract_id === ACTIVE_CLUSTER);
  banner.style.display = '';
  document.getElementById('cbId').textContent = ACTIVE_CLUSTER;
  document.getElementById('cbMeta').textContent = c
    ? `· ${c.node_count} node${c.node_count===1?'':'s'}${c.images && c.images.length ? ' · ' + shortImage(c.images[0]) : ''}`
    : '';
}

async function loadClusters() {
  try {
    CLUSTERS = await fetchJSON('/api/clusters');
    // If active cluster vanished or is unmonitored, fall back to All.
    if (ACTIVE_CLUSTER) {
      const c = CLUSTERS.find(x => x.contract_id === ACTIVE_CLUSTER);
      if (!c || !c.monitored) ACTIVE_CLUSTER = null;
    }
    renderClusters();
    updateClusterBanner();
  } catch (e) {
    const host = document.getElementById('clusterList');
    if (host) host.innerHTML = '<div style="color:var(--bad);font-family:var(--mono);font-size:11px">' + esc(e.message || String(e)) + '</div>';
  }
}

function renderClusters() {
  const host = document.getElementById('clusterList');
  if (!host) return;
  if (!CLUSTERS.length) {
    host.innerHTML = '<div style="color:var(--fg-dim);font-family:var(--mono);font-size:11px">no clusters discovered yet — click <b>discover</b> above</div>';
    return;
  }
  const sorted = CLUSTERS.slice().sort((a,b) =>
    (Number(b.monitored)-Number(a.monitored)) || (b.node_count-a.node_count)
    || ((b.last_seen||0)-(a.last_seen||0))
  );
  const monNodes = sorted.filter(c=>c.monitored).reduce((a,c)=>a+(c.node_count||0),0);
  const monCount = sorted.filter(c=>c.monitored).length;
  let html = '<div class="clist">';
  html += `<div class="crow all-row${ACTIVE_CLUSTER==null?' act':''}">` +
    `<div class="info" data-cid="" title="show every monitored cluster">` +
    `<div class="id-row"><b>All monitored</b><span class="np${monNodes>0?' hot':''}">${monCount} clusters · ${monNodes} nodes</span></div>` +
    `</div></div>`;
  for (const c of sorted) {
    const cls = (c.monitored?'mon ':'') + (ACTIVE_CLUSTER===c.contract_id?'act':'');
    const tenants = (c.tenants||[]).map(shortHash).join(', ');
    const images  = (c.images||[]).map(shortImage).join(', ');
    const seen = c.last_seen ? new Date(c.last_seen*1000).toLocaleString() : '';
    html += `<div class="crow ${cls}">` +
      `<label class="tg" data-cid="${esc(c.contract_id)}" title="${c.monitored?'stop monitoring':'start monitoring'}">` +
        `<input type="checkbox"${c.monitored?' checked':''}><span class="sw"></span></label>` +
      `<div class="info${c.monitored?'':' dis'}" data-cid="${esc(c.contract_id)}" title="${c.monitored?'scope dashboard to this cluster':'monitor this cluster first'}">` +
        `<div class="id-row">` +
          `<span class="cid">${esc(shortCid(c.contract_id))}</span>` +
          `<span class="np${c.node_count>0?' hot':''}">${c.node_count} node${c.node_count===1?'':'s'}</span>` +
        `</div>` +
        `<div class="meta-row">` +
          (tenants?`<span><b>tenant</b>${esc(tenants)}</span>`:'') +
          (images?`<span><b>image</b>${esc(images)}</span>`:'') +
          (seen?`<span><b>seen</b>${esc(seen)}</span>`:'') +
        `</div>` +
      `</div></div>`;
  }
  html += '</div>';
  host.innerHTML = html;
  host.querySelectorAll('.tg input').forEach(inp => {
    inp.onchange = async (e) => {
      const cid = inp.closest('.tg').dataset.cid;
      const want = inp.checked;
      inp.disabled = true;
      try {
        await fetch('/api/clusters/monitor', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({contract_id: cid, monitored: want}),
        });
        await loadClusters();
        if (!want && ACTIVE_CLUSTER === cid) setActiveCluster(null);
        else hardReload();
      } catch (err) {
        alert('cluster toggle failed: ' + err.message);
        inp.checked = !want;
      } finally { inp.disabled = false; }
    };
  });
  host.querySelectorAll('.info[data-cid]').forEach(el => {
    el.onclick = () => {
      const cid = el.dataset.cid;
      if (cid && el.classList.contains('dis')) return;
      setActiveCluster(cid || null);
    };
  });
}

document.getElementById('discoverBtn').onclick = async () => {
  const btn = document.getElementById('discoverBtn');
  const prev = btn.textContent; btn.textContent = 'discovering…'; btn.disabled = true;
  try {
    const r = await fetch('/api/discover_now', { method:'POST' });
    await r.json().catch(()=>({}));
    await loadClusters();
    hardReload();
  } catch (e) { alert('discover failed: ' + e.message); }
  btn.textContent = prev; btn.disabled = false;
};
document.getElementById('cbClear').onclick = () => setActiveCluster(null);

loadClusters();
setInterval(loadClusters, 30000);

// ---- DB tracking policy + db-size badge -----------------------------
async function loadPolicy() {
  try {
    const p = await fetchJSON('/api/policy');
    const sel = document.getElementById('policy');
    sel.innerHTML = '';
    const modes = (p.modes && p.modes.length) ? p.modes : ['balanced','full','minimal'];
    for (const m of modes) {
      const o = document.createElement('option');
      o.value = m; o.textContent = m;
      sel.appendChild(o);
    }
    sel.value = p.mode || 'balanced';
    sel.disabled = false;
    sel.onchange = async () => {
      sel.disabled = true;
      try {
        const r = await fetch('/api/policy', {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({mode: sel.value}),
        });
        const j = await r.json().catch(()=>({}));
        if (!r.ok || j.ok === false) throw new Error(j.error || ('HTTP '+r.status));
      } catch (e) {
        alert('Policy save failed: ' + e.message);
        try { await loadPolicy(); } catch {}
      } finally { sel.disabled = false; }
    };
  } catch (e) { /* leave dropdown disabled */ }
}
async function loadDbSize() {
  try {
    const s = await fetchJSON('/api/db_size');
    const el = document.getElementById('dbSize');
    if (el) el.textContent = '(' + (s.human || '—') + ')';
  } catch {}
}
loadPolicy(); loadDbSize();
setInterval(loadDbSize, 15000);

document.getElementById('reload').onclick = refresh;
document.getElementById('export').onclick = () => {
  const btn = document.getElementById('export');
  const prev = btn.textContent; btn.textContent = 'building…'; btn.disabled = true;
  // navigating triggers the download; re-enable after a moment (we can't observe completion of a navigation download)
  window.location = '/api/report?window=' + windowParam();
  setTimeout(() => { btn.textContent = prev; btn.disabled = false; }, 4000);
};
document.getElementById('clearDb').onclick = async () => {
  const sz = (document.getElementById('dbSize') || {}).textContent || '';
  if (!confirm('Wipe ALL stored data ' + sz + ' — log events, host metrics, error spells + their captured artifacts, instances?\\n\\nLive tailing & metric sampling keep running; history just starts fresh. This cannot be undone.')) return;
  const btn = document.getElementById('clearDb');
  const prev = btn.innerHTML; btn.textContent = 'clearing…'; btn.disabled = true;
  try {
    const r = await fetch('/api/clear', { method: 'POST' });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) throw new Error(j.error || ('HTTP ' + r.status));
    if (j.db_size_human) {
      const el = document.getElementById('dbSize');
      if (el) el.textContent = '(' + j.db_size_human + ')';
    }
  } catch (e) {
    alert('Clear failed: ' + e.message);
    btn.innerHTML = prev; btn.disabled = false; return;
  }
  // reset client-side state and rebuild from the now-empty DB
  OPEN_SPELLS.clear(); SPELL_LIST_KEY = '';
  LAST_SPELLS = []; LAST_PROC_LATEST = {}; LAST_GLOBAL_BUCKETS = []; LAST_INST_BUCKETS = {};
  closeDrawer();
  hardReload();
  btn.innerHTML = prev; btn.disabled = false;
  loadDbSize();
};
function hardReload() {
  for (const k of Object.keys(charts)) { try { charts[k].destroy(); } catch(e) {} }
  charts = {}; LAST_INST_BUCKETS = {};
  if (globalChart) { try { globalChart.destroy(); } catch(e) {} globalChart = null; }
  destroyCharts(() => true);                          // host-panel + inline-spell + drawer sparklines
  document.getElementById('cards').innerHTML = '';
  document.getElementById('spellRows').innerHTML = '';
  refresh();
}
document.getElementById('window').onchange = hardReload;
document.getElementById('bucket').onchange = hardReload;
document.getElementById('refresh').onchange = scheduleRefresh;

renderTagChips();
refresh().catch(e => {
  document.getElementById('cards').innerHTML = '<div class="empty">Error: ' + e.message + '</div>';
});
scheduleRefresh();
</script>
</body>
</html>
"""


def _human_bytes(n: int) -> str:
    """Compact size — 1.2 MB / 850 KB / 12 B."""
    try:
        n = int(n)
    except Exception:
        return "?"
    units = ("B", "KB", "MB", "GB", "TB")
    i = 0
    f = float(n)
    while f >= 1024.0 and i < len(units) - 1:
        f /= 1024.0
        i += 1
    if i == 0:
        return f"{int(f)} {units[i]}"
    return f"{f:.1f} {units[i]}"


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


def make_handler(store: Store, static_html_path: str | None,
                 roundtime_ms: int = DEFAULT_ROUNDTIME_MS,
                 metrics: "MetricsCollector | None" = None,
                 spell_manager: "SpellManager | None" = None,
                 sashi_bin: str = "sashi",
                 report_peers: list | None = None,
                 policy: "PolicyManager | None" = None,
                 discoverer: "Discoverer | None" = None):
    hostname = socket.gethostname()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:  # silence default logger
            pass

        def _send(self, code: int, body: bytes, ctype: str, extra_headers: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
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

            if u.path == "/api/clusters":
                self._json(store.list_clusters())
                return

            if u.path == "/api/summary":
                w = _parse_window(qs.get("window", [None])[0])
                cid = qs.get("contract_id", [None])[0]
                self._json(store.summary(w, roundtime_ms, contract_id=cid))
                return

            if u.path == "/api/events":
                inst = qs.get("instance", [None])[0]
                since = float(qs["since"][0]) if "since" in qs else None
                until = float(qs["until"][0]) if "until" in qs else None
                tag = qs.get("tag", [None])[0]
                limit = int(qs.get("limit", ["2000"])[0])
                cid = qs.get("contract_id", [None])[0]
                self._json(store.events_window(inst, since, until, tag, limit,
                                               contract_id=cid))
                return

            if u.path == "/api/histogram":
                inst = qs.get("instance", [None])[0]
                window = _parse_window(qs.get("window", [None])[0])
                bucket = int(qs.get("bucket", ["60"])[0])
                cid = qs.get("contract_id", [None])[0]
                until = time.time()
                if window <= 0:
                    earliest = store.earliest_event_ts(inst)
                    since = earliest if earliest is not None else until - 60
                else:
                    since = until - window
                self._json(store.histogram(inst, since, until, bucket,
                                           contract_id=cid))
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

            if u.path == "/api/host_metrics":
                inst = qs.get("instance", [None])[0]
                window = _parse_window(qs.get("window", [None])[0])
                until = time.time()
                if window <= 0:
                    since = until - 3600.0
                else:
                    since = until - window
                limit = int(qs.get("limit", ["20000"])[0])
                self._json(store.host_metrics_window(inst, since, until, limit))
                return

            if u.path == "/api/metrics_now":
                # Take an out-of-band sample and return the latest machine row.
                if metrics is not None:
                    metrics.sample_now()
                self._json(store.latest_host_metric() or {})
                return

            if u.path == "/api/spells_log":
                inst = qs.get("instance", [None])[0]
                window = _parse_window(qs.get("window", [None])[0])
                cid = qs.get("contract_id", [None])[0]
                until = time.time()
                since = (until - 86400.0) if window <= 0 else (until - window)
                limit = int(qs.get("limit", ["500"])[0])
                self._json(store.spells_log_window(since, until, inst, limit,
                                                   contract_id=cid))
                return

            if u.path == "/api/spell_artifacts":
                sid = qs.get("spell_id", [None])[0]
                if not sid:
                    self._json({"error": "spell_id required"}, code=400)
                    return
                self._json(store.spell_artifacts(sid))
                return

            if u.path == "/api/open_spells":
                self._json(store.open_spells())
                return

            if u.path == "/api/report":
                window = _parse_window(qs.get("window", ["all"])[0])
                self_only = qs.get("self", ["0"])[0] in ("1", "true", "yes")
                try:
                    text = build_report(
                        store, window, sashi_bin, hostname,
                        report_peers=(None if self_only else report_peers),
                        include_peers=not self_only,
                        roundtime_ms=roundtime_ms,
                    )
                except Exception as e:
                    text = f"REPORT GENERATION FAILED on {hostname}: {e!r}\n"
                fname = f"sashimon-report-{hostname}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.txt"
                self._send(200, text.encode("utf-8"), "text/plain; charset=utf-8",
                           {"Content-Disposition": f'attachment; filename="{fname}"'})
                return

            if u.path == "/api/db_size":
                size = store.db_size_bytes()
                self._json({
                    "bytes": size,
                    "human": _human_bytes(size),
                    "path":  store.path,
                })
                return

            if u.path == "/api/policy":
                self._json({
                    "mode":        policy.mode() if policy else DEFAULT_POLICY_MODE,
                    "modes":       sorted(POLICY_MODES.keys()),
                    "severity":    TAG_SEVERITY,
                    "always_track": sorted(ALWAYS_TRACK_TAGS),
                    "actions":     POLICY_MODES,
                })
                return

            if u.path == "/healthz":
                self._json({"ok": True})
                return

            self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            u = urlparse(self.path)

            if u.path == "/api/clusters/monitor":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length > 0 else b""
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except Exception:
                    self._json({"ok": False, "error": "bad json body"}, code=400)
                    return
                # Accept either { contract_id, monitored } or { contract_ids:[...], monitored }
                cids = body.get("contract_ids")
                if not cids:
                    cid = body.get("contract_id")
                    cids = [cid] if cid else []
                if not cids:
                    self._json({"ok": False, "error": "no contract_id(s)"}, code=400)
                    return
                monitored = bool(body.get("monitored", True))
                for cid in cids:
                    if not isinstance(cid, str):
                        continue
                    store.upsert_cluster(cid)
                    store.set_cluster_monitored(cid, monitored)
                # Trigger an immediate discover pass so tails spawn/reap now.
                if discoverer is not None:
                    discoverer.trigger()
                self._json({"ok": True, "monitored": monitored,
                            "contract_ids": cids})
                return

            if u.path == "/api/discover_now":
                # Drain any body.
                length = int(self.headers.get("Content-Length") or 0)
                if length > 0:
                    try: self.rfile.read(length)
                    except Exception: pass
                if discoverer is None:
                    self._json({"ok": False, "error": "discoverer disabled"},
                               code=500)
                    return
                result = discoverer.discover_once()
                self._json({"ok": "error" not in result, **result})
                return

            if u.path == "/api/policy":
                length = int(self.headers.get("Content-Length") or 0)
                raw = b""
                if length > 0:
                    try:
                        raw = self.rfile.read(length)
                    except Exception:
                        raw = b""
                mode = None
                # Accept either application/json {"mode":"balanced"} or form body.
                ctype = (self.headers.get("Content-Type") or "").lower()
                try:
                    if "application/json" in ctype and raw:
                        body = json.loads(raw.decode("utf-8") or "{}")
                        mode = body.get("mode")
                    else:
                        body = parse_qs(raw.decode("utf-8")) if raw else {}
                        mode = (body.get("mode") or [None])[0]
                except Exception:
                    mode = None
                if not mode:
                    mode = (parse_qs(u.query).get("mode") or [None])[0]
                if not policy:
                    self._json({"ok": False, "error": "policy disabled"}, code=500)
                    return
                try:
                    policy.set_mode(mode)
                except ValueError as e:
                    self._json({"ok": False, "error": str(e)}, code=400)
                    return
                self._json({"ok": True, "mode": policy.mode()})
                return

            if u.path == "/api/clear":
                # Drain any request body to keep the connection clean.
                length = int(self.headers.get("Content-Length") or 0)
                if length > 0:
                    try:
                        self.rfile.read(length)
                    except Exception:
                        pass
                try:
                    result = store.clear_all()
                    # Forget in-memory spell state so a currently-active fork
                    # opens a fresh spells_log row on its next error tag.
                    if spell_manager is not None:
                        try:
                            spell_manager.state.clear()
                        except Exception:
                            pass
                    sz = store.db_size_bytes()
                    self._json({
                        "ok": True,
                        "db_size_bytes": sz,
                        "db_size_human": _human_bytes(sz),
                        **result,
                    })
                except Exception as e:
                    self._json({"ok": False, "error": str(e)}, code=500)
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
    ap.add_argument("--roundtime-ms", type=int,
                    default=int(os.environ.get("SASHIMON_ROUNDTIME_MS",
                                               DEFAULT_ROUNDTIME_MS)),
                    help="HotPocket consensus roundtime in ms; drives the "
                         "uptime-%% denominator. Default 2000.")
    ap.add_argument("--metrics-interval", type=int,
                    default=int(os.environ.get("SASHIMON_METRICS_INTERVAL",
                                               METRICS_INTERVAL)),
                    help="Seconds between host-metric samples (normal). Default 30.")
    ap.add_argument("--metrics-interval-boost", type=int,
                    default=int(os.environ.get("SASHIMON_METRICS_INTERVAL_BOOST",
                                               METRICS_INTERVAL_BOOST)),
                    help="Seconds between host-metric samples while a spell is "
                         "active. Default 3.")
    ap.add_argument("--metrics-boost-cooldown", type=int,
                    default=int(os.environ.get("SASHIMON_METRICS_BOOST_COOLDOWN",
                                               METRICS_BOOST_COOLDOWN)),
                    help="Keep boosted sampling this long after the last error "
                         "tag. Default 120.")
    ap.add_argument("--no-metrics", action="store_true",
                    default=(os.environ.get("SASHIMON_NO_METRICS", "") not in ("", "0", "false")),
                    help="Disable the host-metrics sampler (spell tracking + "
                         "diagnostic snapshots still run).")
    ap.add_argument("--no-ntp", action="store_true",
                    default=(os.environ.get("SASHIMON_NO_NTP", "") not in ("", "0", "false")),
                    help="Skip the chronyc/timedatectl NTP probe in samples.")
    ap.add_argument("--report-peers",
                    default=os.environ.get("SASHIMON_REPORT_PEERS", ""),
                    help="Comma-separated http://host:port of other sashimon "
                         "monitors. When set, GET /api/report (the 'export "
                         "report' button) fetches their reports too, so a "
                         "multi-VM cluster yields one document for every node.")
    ap.add_argument("--policy-mode",
                    default=os.environ.get("SASHIMON_POLICY_MODE", DEFAULT_POLICY_MODE),
                    choices=sorted(POLICY_MODES.keys()),
                    help="DB tracking policy. 'full' tracks everything; "
                         "'balanced' (default) drops low-severity event floods "
                         "during a spell and skips metric boost/snapshot for "
                         "low-impact spells (consensus_lost, out_of_sync, "
                         "warning); 'minimal' only stores fork-class events.")
    ap.add_argument("--tls-cert",
                    default=os.environ.get("SASHIMON_TLS_CERT", ""),
                    help="Path to TLS certificate (PEM). When set together "
                         "with --tls-key, the dashboard is served over HTTPS.")
    ap.add_argument("--tls-key",
                    default=os.environ.get("SASHIMON_TLS_KEY", ""),
                    help="Path to TLS private key (PEM).")
    ap.add_argument("--tls-auto", action="store_true",
                    default=(os.environ.get("SASHIMON_TLS_AUTO", "") not in ("", "0", "false")),
                    help="If --tls-cert/--tls-key are blank, try the Sashimono "
                         "contract template defaults "
                         "(/etc/sashimono/contract_template/cfg/tls{cert,key}.pem). "
                         "Silently falls back to plain HTTP if those don't exist.")
    ap.add_argument("--auto-monitor-new", action="store_true",
                    default=(os.environ.get("SASHIMON_AUTO_MONITOR_NEW", "") not in ("", "0", "false")),
                    help="Automatically start monitoring newly-discovered "
                         "clusters. Default off: operator opts in per cluster "
                         "from the dashboard. Existing installs that want the "
                         "old 'tail everything' behaviour can set this.")
    args = ap.parse_args()

    sashi_path = shutil.which(args.sashi) or args.sashi
    report_peers = [p.strip() for p in (args.report_peers or "").split(",") if p.strip()]
    store = Store(args.db)
    stop = threading.Event()
    tails: dict[str, Tail] = {}
    policy = PolicyManager(store, default_mode=args.policy_mode)

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

    metrics: MetricsCollector | None = None
    if not args.no_metrics:
        metrics = MetricsCollector(
            store, stop, tails,
            normal_interval=args.metrics_interval,
            boost_interval=args.metrics_interval_boost,
            ntp_enabled=not args.no_ntp,
        )
    spell_mgr = SpellManager(
        store, stop, sashi_path, metrics, tails,
        boost_cooldown=args.metrics_boost_cooldown,
        policy=policy,
    )
    if metrics is not None:
        metrics.tick_cb = spell_mgr.tick
        metrics.start()

    discoverer = Discoverer(store, tails, stop, sashi_path,
                            spell_manager=spell_mgr, policy=policy,
                            auto_monitor_new=args.auto_monitor_new)
    discoverer.start()
    Pruner(store, stop, args.retention_days).start()

    server = ThreadingHTTPServer(
        (args.bind, args.port),
        make_handler(store, static_path or None,
                     roundtime_ms=args.roundtime_ms, metrics=metrics,
                     spell_manager=spell_mgr, sashi_bin=sashi_path,
                     report_peers=report_peers, policy=policy,
                     discoverer=discoverer),
    )

    # ---- TLS wrap (optional) ------------------------------------------
    tls_cert = (args.tls_cert or "").strip()
    tls_key = (args.tls_key or "").strip()
    if not tls_cert and not tls_key and args.tls_auto:
        c = "/etc/sashimono/contract_template/cfg/tlscert.pem"
        k = "/etc/sashimono/contract_template/cfg/tlskey.pem"
        if os.path.isfile(c) and os.path.isfile(k):
            tls_cert, tls_key = c, k
    scheme = "http"
    if tls_cert and tls_key:
        try:
            import ssl
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(tls_cert, tls_key)
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
            scheme = "https"
            print(f"[sashimon] TLS enabled  cert={tls_cert}  key={tls_key}")
        except Exception as e:
            print(f"[sashimon] TLS init failed ({e}) — falling back to HTTP",
                  file=sys.stderr)
            scheme = "http"

    print(f"[sashimon] db={args.db}  sashi={sashi_path}")
    print(f"[sashimon] static={static_path or '(embedded fallback)'}")
    print(f"[sashimon] policy={policy.mode()}")
    print(f"[sashimon] roundtime={args.roundtime_ms}ms  "
          f"metrics={'off' if args.no_metrics else f'{args.metrics_interval}s/{args.metrics_interval_boost}s'}"
          f"  report-peers={report_peers or '(none)'}")
    print(f"[sashimon] dashboard {scheme}://{args.bind}:{args.port}")
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
