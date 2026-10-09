#!/usr/bin/env python3
"""sashimon - HotPocket consensus observer for Sashimono hosts.

Follows every HotPocket instance's own log file (hp.log, never docker attach, so a slow monitor can
never block a node), turns log lines into compact per-node/per-minute records, detects consensus
incidents (forks, stalls, height splits, frozen nodes, refused closes, UNL switches...), keeps the raw
log only in a short in-memory ring buffer that is saved when an incident fires, and serves a dashboard.
Peers (other hosts running sashimon) are polled so each host sees the whole cluster.

Standard library only.
"""

import argparse
import collections
import glob
import json
import os
import queue
import re
import shutil
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2.0.0"

PEER_STALE_S = 60     # keep using a peer's last good snapshot this long after a failed poll

# ---------------------------------------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------------------------------------

LINE_RE = re.compile(r"^(\d{8}) (\d\d:\d\d:\d\d\.\d{3}) \[(\w{3})\]\[(\w+)\] (.*)$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

RE_CREATED = re.compile(r"\*\*\*\*Ledger created\*\*\*\* \(lcl:(\d+)-([0-9a-f]+) state:([0-9a-f]+) patch:([0-9a-f]+)")
RE_WON = re.compile(r"won:(\d+) needed:(\d+)")
RE_VOTES = re.compile(r"votes:(\d+) needed:(\d+)")
RE_RECEIVED = re.compile(r"received:(\d+) needed:(\d+)")
RE_MISSED = re.compile(r"Missed stage (\d) window")
RE_SKIPPED = re.compile(r"Skipped (\d+) round")
RE_VOTE_STATUS = re.compile(r"^Vote status: (\d)")
RE_STATS = re.compile(r"Consensus stats \(last (\d+) rounds\): (.*)$")
RE_KV = re.compile(r"([a-z-]+):(\d+)")
RE_FF = re.compile(r"ours:(\d+)-([0-9a-f]+) certified:(\d+)-([0-9a-f]+) from:([0-9a-f]+)")
RE_REFUSED = re.compile(r"lcl\(ours/theirs\):(\d+)-([0-9a-f]+)/(\d+)-([0-9a-f]+)")
RE_VERSION = re.compile(r"^HotPocket (\d+\.\d+\.\d+)")
RE_PUBKEY = re.compile(r"^Public key: (ed[0-9a-f]+)")
RE_PATCH_SYNC = re.compile(r"Applying pending patch recorded in synced ledger (\d+)-([0-9a-f]+)")
RE_CONFLICT = re.compile(r"Conflicting certified ledgers at seq (\d+)")

VOTE_STATUS_NAMES = {0: "unknown", 1: "unreliable", 2: "desync", 3: "synced"}

# Debug lines emitted many times per round that carry nothing we record (checked first, cheaply).
HIGH_VOLUME_PREFIXES = ("[s", "Erased [s", "Waiting ", "Started stage", "Proposed-s", "Serving hpfs",
                        "Hpfs ldgr serve", "Hpfs cont serve", "Starting hpfs", "Stopping hpfs", "Closing ledger with")

# Per-minute counters kept per node.
MINUTE_FIELDS = [
    "lines", "closes", "max_seq", "vote_split", "few_votes", "consensus_lost", "shard_split", "desync",
    "missed_stage", "skipped_rounds", "refused", "late_close", "fast_forward", "sync_target", "errors",
    "warnings", "queue_full", "frozen_s", "unreliable",
]


def parse_ts(d, t):
    # HotPocket logs in the container's local time, which is UTC on Sashimono instances.
    return datetime.strptime(d + " " + t, "%Y%m%d %H:%M:%S.%f").replace(tzinfo=timezone.utc).timestamp()


# ---------------------------------------------------------------------------------------------------------
# Storage: one writer thread, batched transactions, bounded size
# ---------------------------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS clusters (
    id INTEGER PRIMARY KEY, contract_id TEXT UNIQUE NOT NULL, first_seen REAL, last_seen REAL);
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY, cluster_id INTEGER, name TEXT NOT NULL, short TEXT, host TEXT,
    pubkey TEXT, version TEXT, image TEXT, first_seen REAL, last_seen REAL, UNIQUE (cluster_id, name));
CREATE TABLE IF NOT EXISTS node_minutes (
    node_id INTEGER, minute INTEGER,
    lines INTEGER, closes INTEGER, max_seq INTEGER, vote_split INTEGER, few_votes INTEGER,
    consensus_lost INTEGER, shard_split INTEGER, desync INTEGER, missed_stage INTEGER,
    skipped_rounds INTEGER, refused INTEGER, late_close INTEGER, fast_forward INTEGER, sync_target INTEGER,
    errors INTEGER, warnings INTEGER, queue_full INTEGER, frozen_s INTEGER, unreliable INTEGER,
    PRIMARY KEY (node_id, minute)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_nm_minute ON node_minutes(minute);
CREATE TABLE IF NOT EXISTS ledgers (
    cluster_id INTEGER, seq INTEGER, hash TEXT, nodes TEXT, first_ts REAL, last_ts REAL,
    PRIMARY KEY (cluster_id, seq, hash)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY, cluster_id INTEGER, node_id INTEGER, kind TEXT, severity TEXT,
    start_ts REAL, end_ts REAL, summary TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_inc_cluster_ts ON incidents(cluster_id, start_ts);
CREATE TABLE IF NOT EXISTS captures (
    id INTEGER PRIMARY KEY, incident_id INTEGER, node_id INTEGER, ts REAL, kind TEXT, data BLOB);
CREATE INDEX IF NOT EXISTS idx_cap_incident ON captures(incident_id);
CREATE TABLE IF NOT EXISTS stats (
    node_id INTEGER, ts REAL, rounds INTEGER, data TEXT, PRIMARY KEY (node_id, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS host_minutes (
    minute INTEGER PRIMARY KEY, load1 REAL, cpu REAL, iowait REAL, mem_pct REAL, swap_mb REAL,
    disk_free_mb REAL, sashimon_cpu REAL);
"""


class Store:
    def __init__(self, path, max_mb, retention_h):
        self.path = path
        self.max_mb = max_mb
        self.retention_s = retention_h * 3600
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        new = not os.path.exists(path)
        self.w = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        if new:
            self.w.execute("PRAGMA auto_vacuum=INCREMENTAL")
        self.w.execute("PRAGMA journal_mode=WAL")
        self.w.execute("PRAGMA synchronous=NORMAL")
        self.w.execute("PRAGMA journal_size_limit=67108864")
        self.w.execute("PRAGMA busy_timeout=5000")
        self.w.executescript(SCHEMA)
        # Incidents left open by a previous run can't be tracked any more: close them at restart.
        self.w.execute("UPDATE incidents SET end_ts=?, summary=summary || ' (closed at monitor restart)' "
                       "WHERE end_ts IS NULL", (time.time(),))
        self.q = queue.Queue(maxsize=50000)
        self.ids = {}            # ("cluster", contract_id) / ("node", name) -> id
        self.id_lock = threading.Lock()
        self.dropped = 0
        threading.Thread(target=self._writer, name="db-writer", daemon=True).start()

    # -- ids are assigned synchronously (rare) so callers can use them immediately
    def cluster_id(self, contract_id):
        key = ("c", contract_id)
        with self.id_lock:
            if key in self.ids:
                return self.ids[key]
            now = time.time()
            self.w.execute("INSERT OR IGNORE INTO clusters(contract_id, first_seen, last_seen) VALUES (?,?,?)",
                           (contract_id, now, now))
            cid = self.w.execute("SELECT id FROM clusters WHERE contract_id=?", (contract_id,)).fetchone()[0]
            self.ids[key] = cid
            return cid

    def node_id(self, name, cluster_id, host, image):
        key = ("n", cluster_id, name)
        with self.id_lock:
            if key in self.ids:
                return self.ids[key]
            now = time.time()
            self.w.execute("INSERT OR IGNORE INTO nodes(cluster_id, name, short, host, image, first_seen, last_seen) "
                           "VALUES (?,?,?,?,?,?,?)", (cluster_id, name, name[:8], host, image, now, now))
            nid = self.w.execute("SELECT id FROM nodes WHERE cluster_id=? AND name=?", (cluster_id, name)).fetchone()[0]
            self.ids[key] = nid
            return nid

    def insert_incident(self, cluster_id, node_id, kind, severity, start_ts, summary, detail):
        with self.id_lock:
            cur = self.w.execute(
                "INSERT INTO incidents(cluster_id, node_id, kind, severity, start_ts, end_ts, summary, detail) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (cluster_id, node_id, kind, severity, start_ts, None, summary, json.dumps(detail)))
            return cur.lastrowid

    def put(self, sql, args):
        try:
            self.q.put_nowait((sql, args))
        except queue.Full:
            self.dropped += 1

    def _writer(self):
        while True:
            batch = [self.q.get()]
            deadline = time.time() + 1.0
            while time.time() < deadline and len(batch) < 5000:
                try:
                    batch.append(self.q.get(timeout=max(0.01, deadline - time.time())))
                except queue.Empty:
                    break
            try:
                with self.id_lock:
                    self.w.execute("BEGIN")
                    for sql, args in batch:
                        self.w.execute(sql, args)
                    self.w.execute("COMMIT")
            except Exception:
                traceback.print_exc()
                try:
                    self.w.execute("ROLLBACK")
                except Exception:
                    pass

    def reader(self):
        c = sqlite3.connect("file:%s?mode=ro" % self.path, uri=True, timeout=10, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    def size_bytes(self):
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return total

    def maintain(self):
        """Retention by age, then by size; reclaim space; keep the WAL small."""
        cutoff = time.time() - self.retention_s
        minute_cut = int(cutoff // 60)
        stmts = [
            ("DELETE FROM node_minutes WHERE minute < ?", (minute_cut,)),
            ("DELETE FROM host_minutes WHERE minute < ?", (minute_cut,)),
            ("DELETE FROM ledgers WHERE last_ts < ?", (cutoff,)),
            ("DELETE FROM stats WHERE ts < ?", (cutoff,)),
            ("DELETE FROM captures WHERE ts < ?", (cutoff,)),
            ("DELETE FROM incidents WHERE start_ts < ? AND end_ts IS NOT NULL", (cutoff,)),
        ]
        with self.id_lock:
            for sql, args in stmts:
                self.w.execute(sql, args)
            # Size cap: captures are the bulk; drop the oldest until under the cap.
            for _ in range(20):
                if self.size_bytes() <= self.max_mb * 1048576:
                    break
                row = self.w.execute("SELECT MIN(id) FROM captures").fetchone()
                if not row or row[0] is None:
                    break
                self.w.execute("DELETE FROM captures WHERE id < ?", (row[0] + 200,))
                self.w.execute("PRAGMA incremental_vacuum(2000)")
            self.w.execute("PRAGMA incremental_vacuum(5000)")
            self.w.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def purge_cluster(self, cluster_id):
        """Delete everything stored about one cluster (used once it no longer exists)."""
        with self.id_lock:
            nodes = "(SELECT id FROM nodes WHERE cluster_id=?)"
            for sql in (
                "DELETE FROM captures WHERE incident_id IN (SELECT id FROM incidents WHERE cluster_id=?)",
                "DELETE FROM incidents WHERE cluster_id=?",
                "DELETE FROM node_minutes WHERE node_id IN " + nodes,
                "DELETE FROM stats WHERE node_id IN " + nodes,
                "DELETE FROM ledgers WHERE cluster_id=?",
                "DELETE FROM nodes WHERE cluster_id=?",
                "DELETE FROM clusters WHERE id=?",
            ):
                self.w.execute(sql, (cluster_id,))
            self.w.execute("PRAGMA incremental_vacuum(2000)")
            for key in [k for k, v in self.ids.items() if (k[0] == "c" and v == cluster_id) or
                        (k[0] == "n" and k[1] == cluster_id)]:
                del self.ids[key]

    def clear(self):
        with self.id_lock:
            for t in ("node_minutes", "host_minutes", "ledgers", "stats", "captures", "incidents"):
                self.w.execute("DELETE FROM %s" % t)
            self.w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.w.execute("VACUUM")
            self.w.execute("PRAGMA wal_checkpoint(TRUNCATE)")


# ---------------------------------------------------------------------------------------------------------
# Per-node state and log following
# ---------------------------------------------------------------------------------------------------------

class Node:
    def __init__(self, app, name, contract_id, user, log_dir, image, cfg_path):
        self.app = app
        self.name = name
        self.short = name[:8]
        self.contract_id = contract_id
        self.user = user
        self.log_dir = log_dir
        self.image = image
        self.cfg_path = cfg_path
        self.cluster_id = app.store.cluster_id(contract_id)
        self.id = app.store.node_id(name, self.cluster_id, app.host, image)
        self.version = None
        self.pubkey = None
        self.seq = None
        self.hash = None
        self.close_ts = None
        self.line_ts = None          # hpcore timestamp of the last line
        self.read_ts = None          # wall time the last line was read
        self.vote_status = None
        self.last_votes = None       # e.g. "votes:8/11" or "won:10/11"
        self.stats = None
        self.frozen_since = None
        self.proc = None             # last /proc diagnosis
        self.ring = collections.deque(maxlen=app.ring_lines)
        self.minute = None
        self.cnt = None
        self.stop = threading.Event()
        # Re-entrant: an event raised while parsing a line (under this lock) captures this node's ring buffer.
        self.lock = threading.RLock()
        self.thread = threading.Thread(target=self._follow, name="follow-" + self.short, daemon=True)
        self.thread.start()

    # -- following hp.log across plog rotations (hp.log -> hp.1.log ...)
    def _follow(self):
        path = os.path.join(self.log_dir, "hp.log")
        f = None
        ino = None
        buf = b""
        while not self.stop.is_set():
            try:
                if f is None:
                    if not os.path.exists(path):
                        time.sleep(2)
                        continue
                    f = open(path, "rb")
                    ino = os.fstat(f.fileno()).st_ino
                    if self.read_ts is None:
                        # Start near the end, but read enough to learn version/pubkey/last ledger.
                        size = os.path.getsize(path)
                        f.seek(max(0, size - 262144))
                        self._prime()
                        if size > 262144:
                            f.readline()
                chunk = f.read(65536)
                if chunk:
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for raw in lines:
                        self._line(raw.decode("utf-8", "replace"))
                    continue
                # No new data: rotated or truncated?
                try:
                    st = os.stat(path)
                    if st.st_ino != ino or st.st_size < f.tell():
                        f.close()
                        f = None
                        buf = b""
                        continue
                except FileNotFoundError:
                    pass
                time.sleep(0.25)
            except Exception:
                traceback.print_exc()
                try:
                    if f:
                        f.close()
                except Exception:
                    pass
                f = None
                time.sleep(2)
        if f:
            f.close()

    def _prime(self):
        """Learn version and pubkey from the oldest rotated log (they are only printed at startup)."""
        files = sorted(glob.glob(os.path.join(self.log_dir, "hp.*.log")),
                       key=lambda p: int(re.search(r"hp\.(\d+)\.log$", p).group(1)), reverse=True)
        files.append(os.path.join(self.log_dir, "hp.log"))
        for p in files[:2] + files[-1:]:
            try:
                with open(p, "rb") as fh:
                    head = fh.read(65536).decode("utf-8", "replace").replace("﻿", "")
                for raw in head.splitlines():
                    m = LINE_RE.match(raw)
                    if not m:
                        continue
                    msg = m.group(5)
                    v = RE_VERSION.match(msg)
                    if v:
                        self.version = v.group(1)
                    k = RE_PUBKEY.match(msg)
                    if k:
                        self.pubkey = k.group(1)
                if self.version and self.pubkey:
                    break
            except OSError:
                pass
        if self.version or self.pubkey:
            self.app.store.put("UPDATE nodes SET version=coalesce(?,version), pubkey=coalesce(?,pubkey) WHERE id=?",
                               (self.version, self.pubkey, self.id))

    def _bump(self, ts, field, n=1):
        minute = int(ts // 60)
        if self.minute != minute:
            self._flush()
            self.minute = minute
            self.cnt = dict.fromkeys(MINUTE_FIELDS, 0)
        if field == "max_seq":
            self.cnt["max_seq"] = max(self.cnt["max_seq"], n)
        else:
            self.cnt[field] += n

    def _flush(self):
        if self.minute is None or not self.cnt:
            return
        c = self.cnt
        self.app.store.put(
            "INSERT OR REPLACE INTO node_minutes(node_id, minute, %s) VALUES (?,?,%s)"
            % (",".join(MINUTE_FIELDS), ",".join("?" * len(MINUTE_FIELDS))),
            (self.id, self.minute, *[c[k] for k in MINUTE_FIELDS]))

    def _line(self, raw):
        raw = ANSI_RE.sub("", raw).rstrip("\r").lstrip("﻿")   # plog starts each file with a BOM
        m = LINE_RE.match(raw)
        now = time.time()
        with self.lock:
            self.ring.append(raw)
            self.read_ts = now
            if not m:
                return
            d, t, lvl, _mod, msg = m.groups()
            ts = parse_ts(d, t)
            self.line_ts = ts
            if self.frozen_since:
                self.app.detector.node_unfrozen(self, ts)
                self.frozen_since = None
            self._bump(ts, "lines")
            if lvl == "err":
                self._bump(ts, "errors")
            elif lvl == "wrn":
                self._bump(ts, "warnings")
            self._parse(ts, lvl, msg)

    def _parse(self, ts, lvl, msg):
        c0 = msg[:1]
        if c0 == "*" and "Ledger created" in msg:
            mm = RE_CREATED.search(msg)
            if mm:
                seq, h = int(mm.group(1)), mm.group(2)
                self.seq, self.hash, self.close_ts = seq, h, ts
                self._bump(ts, "closes")
                self._bump(ts, "max_seq", seq)
                self.app.detector.ledger(self, seq, h, ts)
            return
        if msg.startswith(HIGH_VOLUME_PREFIXES):
            # High-volume debug lines (candidate proposals, waits, stage starts, own proposals): nothing to record.
            return
        if msg.startswith("Skipped "):
            mm = RE_SKIPPED.match(msg)
            if mm:
                self._bump(ts, "skipped_rounds", int(mm.group(1)))
            return
        if msg.startswith("Vote status: "):
            mm = RE_VOTE_STATUS.match(msg)
            if mm:
                self.vote_status = int(mm.group(1))
                if self.vote_status == 1:
                    self._bump(ts, "unreliable")
            return
        if msg.startswith("Missed stage"):
            self._bump(ts, "missed_stage")
            return
        if msg.startswith("Cannot close ledger"):
            mm = RE_WON.search(msg)
            self._bump(ts, "vote_split")
            if mm:
                self.last_votes = "won %s/%s" % mm.groups()
            return
        if msg.startswith("Not enough stage 3"):
            self._bump(ts, "few_votes")
            return
        if msg.startswith("Not enough peers proposing"):
            mm = RE_VOTES.search(msg)
            self._bump(ts, "consensus_lost")
            if mm:
                self.last_votes = "votes %s/%s" % mm.groups()
            return
        if msg.startswith("No consensus on last shard hash"):
            mm = RE_WON.search(msg)
            self._bump(ts, "shard_split")
            if mm:
                self.last_votes = "shard %s/%s" % mm.groups()
            return
        if msg.startswith("We are not on the consensus ledger"):
            self._bump(ts, "desync")
            return
        if msg.startswith("Hpfs ldgr sync: Target added") or msg.startswith("Hpfs cont sync: Target added"):
            self._bump(ts, "sync_target")
            return
        if msg.startswith("Proposal queue full") or msg.startswith("Proposal rejected. Maximum proposal count"):
            self._bump(ts, "queue_full")
            return
        if msg.startswith("Ledger closed after waiting"):
            self._bump(ts, "late_close")
            return
        if msg.startswith("Peer proved a closed ledger"):
            self._bump(ts, "fast_forward")
            mm = RE_FF.search(msg)
            self.app.detector.node_event(self, ts, "fast_forward", "info",
                                         "fast-forward %s -> %s" % ((mm.group(1), mm.group(3)) if mm else ("?", "?")),
                                         {"line": msg}, dedupe_s=30)
            return
        if msg.startswith("Winning proposal was not built"):
            self._bump(ts, "refused")
            mm = RE_REFUSED.search(msg)
            self.app.detector.node_event(self, ts, "refused_close", "warn",
                                         "refused close (would have forked)%s" % (
                                             ": ours %s, votes on %s" % (mm.group(1), mm.group(3)) if mm else ""),
                                         {"line": msg}, dedupe_s=20)
            return
        if msg.startswith("Consensus stats"):
            mm = RE_STATS.search(msg)
            if mm:
                data = {k: int(v) for k, v in RE_KV.findall(mm.group(2))}
                self.stats = dict(data, ts=ts)
                self.app.store.put("INSERT OR REPLACE INTO stats(node_id, ts, rounds, data) VALUES (?,?,?,?)",
                                   (self.id, ts, int(mm.group(1)), json.dumps(data)))
            return
        if msg.startswith("Contract config updated from patch file"):
            self.app.detector.node_event(self, ts, "config_patch", "info", "config patch applied (UNL/settings)",
                                         {"line": msg}, dedupe_s=5)
            return
        if msg.startswith("Applying pending patch"):
            mm = RE_PATCH_SYNC.search(msg)
            self.app.detector.node_event(self, ts, "patch_after_sync", "info",
                                         "pending patch applied after sync%s" % (" at %s" % mm.group(1) if mm else ""),
                                         {"line": msg}, dedupe_s=5)
            return
        if msg.startswith("Invalid ledger certificate"):
            self.app.detector.node_event(self, ts, "invalid_cert", "high", "invalid ledger certificate", {"line": msg},
                                         dedupe_s=60)
            return
        if msg.startswith("Conflicting certified ledgers"):
            self.app.detector.node_event(self, ts, "conflicting_cert", "critical", msg[:200], {"line": msg}, dedupe_s=60)
            return
        if c0 == "H" and msg.startswith("HotPocket "):
            mm = RE_VERSION.match(msg)
            if mm:
                self.version = mm.group(1)
                self.app.store.put("UPDATE nodes SET version=? WHERE id=?", (self.version, self.id))
                self.app.detector.node_event(self, ts, "node_start", "info", "HotPocket %s started" % self.version,
                                             {}, dedupe_s=5)
            return
        if msg.startswith("Public key: "):
            mm = RE_PUBKEY.match(msg)
            if mm:
                self.pubkey = mm.group(1)
                self.app.store.put("UPDATE nodes SET pubkey=? WHERE id=?", (self.pubkey, self.id))
            return
        if lvl == "err":
            self.app.detector.node_event(self, ts, "error", "warn", msg[:200], {"line": msg}, dedupe_s=60)

    def snapshot(self):
        now = time.time()
        with self.lock:
            return {
                "name": self.name, "short": self.short, "host": self.app.host, "contract_id": self.contract_id,
                "version": self.version, "pubkey": self.pubkey, "image": self.image,
                "seq": self.seq, "hash": self.hash, "close_ts": self.close_ts, "line_ts": self.line_ts,
                "read_ts": self.read_ts, "idle_s": round(now - self.read_ts, 1) if self.read_ts else None,
                "vote_status": VOTE_STATUS_NAMES.get(self.vote_status), "last_votes": self.last_votes,
                "frozen_since": self.frozen_since, "proc": self.proc, "stats": self.stats,
            }

    def ring_text(self, since_ts=None):
        with self.lock:
            lines = list(self.ring)
        return "\n".join(lines)


def find_hpcore_pid(user):
    try:
        uid = int(subprocess.run(["id", "-u", user], capture_output=True, text=True, timeout=5).stdout.strip())
    except Exception:
        return None
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if os.stat("/proc/" + pid).st_uid != uid:
                continue
            with open("/proc/%s/cmdline" % pid, "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ")
            if b"hpcore run" in cmd:
                return int(pid)
        except OSError:
            continue
    return None


def diagnose_proc(user):
    """Where are the hpcore threads waiting? A thread inside write() to fd 1 means stdout is not being drained."""
    pid = find_hpcore_pid(user)
    if not pid:
        return {"pid": None}
    threads = []
    blocked_stdout = False
    for t in sorted(os.listdir("/proc/%d/task" % pid)):
        base = "/proc/%d/task/%s/" % (pid, t)
        try:
            wchan = open(base + "wchan").read().strip()
            sysc = open(base + "syscall").read().split()
            state = [l for l in open(base + "status") if l.startswith("State:")][0].split()[1]
        except OSError:
            continue
        nr = sysc[0] if sysc else ""
        arg0 = sysc[1] if len(sysc) > 1 else ""
        if nr == "1" and arg0 in ("0x1", "0x2"):
            blocked_stdout = True
        threads.append({"tid": int(t), "state": state, "wchan": wchan, "syscall": nr, "arg0": arg0})
    try:
        stdout = os.readlink("/proc/%d/fd/1" % pid)
    except OSError:
        stdout = None
    return {"pid": pid, "stdout": stdout, "blocked_in_stdout_write": blocked_stdout, "threads": threads}


# ---------------------------------------------------------------------------------------------------------
# Incident detection (node-level from log lines, cluster-level from the merged multi-host view)
# ---------------------------------------------------------------------------------------------------------

class Detector:
    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.recent = {}          # (cluster_id, seq) -> {hash: set(names)} for local nodes, last ~512 seqs
        self.open = {}            # (cluster_id, kind, key) -> incident id
        self.last_event = {}      # (node name, kind) -> ts
        self.cluster_state = {}   # contract_id -> {"max_seq", "advance_ts"}

    # -- local ledgers
    def ledger(self, node, seq, h, ts):
        with self.lock:
            hashes = self.recent.setdefault((node.cluster_id, seq), {})
            hashes.setdefault(h, set()).add(node.short)
            if len(self.recent) > 4096:
                for k in sorted(self.recent)[:1024]:
                    del self.recent[k]
            nodes = ",".join(sorted(hashes[h]))
        self.app.store.put(
            "INSERT INTO ledgers(cluster_id, seq, hash, nodes, first_ts, last_ts) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(cluster_id, seq, hash) DO UPDATE SET nodes=excluded.nodes, last_ts=excluded.last_ts",
            (node.cluster_id, seq, h, nodes, ts, ts))

    def local_recent(self, cluster_id, min_seq):
        with self.lock:
            return {seq: {h: sorted(n) for h, n in v.items()}
                    for (cid, seq), v in self.recent.items() if cid == cluster_id and seq >= min_seq}

    # -- node events
    def node_event(self, node, ts, kind, severity, summary, detail, dedupe_s=0):
        key = (node.name, kind)
        last = self.last_event.get(key)
        if last and ts - last < dedupe_s:
            return None
        self.last_event[key] = ts
        iid = self.app.store.insert_incident(node.cluster_id, node.id, kind, severity, ts, summary, detail)
        self.app.store.put("UPDATE incidents SET end_ts=? WHERE id=?", (ts, iid))
        if severity in ("high", "critical", "warn") and kind != "error":
            self.capture(iid, node.cluster_id, [node])
        return iid

    def node_unfrozen(self, node, ts):
        k = (node.cluster_id, "freeze", node.name)
        iid = self.open.pop(k, None)
        if iid:
            self.app.store.put("UPDATE incidents SET end_ts=? WHERE id=?", (ts, iid))

    # -- cluster incidents (open/close)
    def _open(self, cluster_id, kind, key, severity, summary, detail, node_id=None, capture_nodes=None):
        k = (cluster_id, kind, key)
        if k in self.open:
            return self.open[k]
        iid = self.app.store.insert_incident(cluster_id, node_id, kind, severity, time.time(), summary, detail)
        self.open[k] = iid
        if capture_nodes:
            self.capture(iid, cluster_id, capture_nodes)
        return iid

    def _close(self, cluster_id, kind, key):
        iid = self.open.pop((cluster_id, kind, key), None)
        if iid:
            self.app.store.put("UPDATE incidents SET end_ts=? WHERE id=?", (time.time(), iid))

    def capture(self, iid, cluster_id, nodes):
        now = time.time()
        for n in nodes:
            text = n.ring_text()
            if text:
                self.app.store.put("INSERT INTO captures(incident_id, node_id, ts, kind, data) VALUES (?,?,?,?,?)",
                                   (iid, n.id, now, "log", zlib.compress(text.encode(), 6)))

    def forget_cluster(self, cluster_id, contract_id):
        with self.lock:
            for k in [k for k in self.recent if k[0] == cluster_id]:
                del self.recent[k]
        for k in [k for k in self.open if k[0] == cluster_id]:
            del self.open[k]
        self.cluster_state.pop(contract_id, None)

    # -- periodic evaluation over the merged (all hosts) view
    def evaluate(self):
        app = self.app
        now = time.time()
        local = app.local_nodes()
        merged = app.merged_nodes()
        by_cluster = collections.defaultdict(list)
        for n in merged:
            by_cluster[n["contract_id"]].append(n)

        # Frozen local nodes: hpcore process alive but no log line for a while.
        for node in local:
            idle = now - node.read_ts if node.read_ts else None
            if idle is not None and idle > app.freeze_s and not node.frozen_since:
                node.frozen_since = node.line_ts or node.read_ts
                node.proc = diagnose_proc(node.user) if node.user else None
                blocked = bool(node.proc and node.proc.get("blocked_in_stdout_write"))
                iid = self._open(node.cluster_id, "freeze", node.name, "high",
                                 "node %s silent for %ds%s" % (node.short, idle,
                                                              " - blocked writing stdout" if blocked else ""),
                                 {"node": node.name, "proc": node.proc}, node_id=node.id, capture_nodes=[node])
                if node.proc:
                    app.store.put("INSERT INTO captures(incident_id, node_id, ts, kind, data) VALUES (?,?,?,?,?)",
                                  (iid, node.id, now, "proc", zlib.compress(json.dumps(node.proc).encode())))
            if node.frozen_since:
                with node.lock:
                    node._bump(now, "frozen_s", 5)

        # Clusters that no longer have any live node: close whatever is still open for them.
        live_cids = {app.store.cluster_id(c) for c in by_cluster}
        for (cid, kind, key) in list(self.open):
            if cid not in live_cids:
                self._close(cid, kind, key)

        for contract_id, nodes in by_cluster.items():
            cid = app.store.cluster_id(contract_id)
            local_cluster_nodes = [n for n in local if n.contract_id == contract_id]
            alive = [n for n in nodes if n.get("idle_s") is not None and n["idle_s"] < app.freeze_s]
            seqs = [n["seq"] for n in nodes if n.get("seq") is not None]
            if not seqs:
                continue
            max_seq = max(seqs)
            st = self.cluster_state.setdefault(contract_id, {"max_seq": max_seq, "advance_ts": now})
            if max_seq > st["max_seq"]:
                st["max_seq"], st["advance_ts"] = max_seq, now

            # Stall: nobody closed a ledger for stall_s.
            stalled_for = now - st["advance_ts"]
            if stalled_for > app.stall_s:
                self._open(cid, "stall", "", "critical",
                           "no ledger closed for %ds (stuck at %d)" % (stalled_for, max_seq),
                           {"max_seq": max_seq, "heights": {n["short"]: n.get("seq") for n in nodes},
                            "votes": {n["short"]: n.get("last_votes") for n in nodes}},
                           capture_nodes=local_cluster_nodes)
            else:
                self._close(cid, "stall", "")

            # Height split: live nodes more than split_seq ledgers behind the leader.
            behind = sorted(n["short"] for n in alive if n.get("seq") is not None and max_seq - n["seq"] > app.split_seq)
            if behind:
                self._open(cid, "height_split", "", "high",
                           "%d node(s) more than %d ledgers behind %d" % (len(behind), app.split_seq, max_seq),
                           {"max_seq": max_seq, "behind": behind,
                            "heights": {n["short"]: n.get("seq") for n in nodes}},
                           capture_nodes=[n for n in local_cluster_nodes if n.short in behind])
            else:
                self._close(cid, "height_split", "")

            # Forks: the same ledger number with different hashes (across all hosts).
            for seq, hashes in app.merged_recent(contract_id, max_seq - 256).items():
                if len(hashes) > 1:
                    key = str(seq)
                    if (cid, "fork", key) not in self.open:
                        self._open(cid, "fork", key, "critical",
                                   "fork at ledger %d: %s" % (seq, " vs ".join(
                                       "%s(%d)" % (h, len(ns)) for h, ns in hashes.items())),
                                   {"seq": seq, "hashes": hashes}, capture_nodes=local_cluster_nodes)


# ---------------------------------------------------------------------------------------------------------
# Host metrics
# ---------------------------------------------------------------------------------------------------------

class HostMetrics:
    def __init__(self, app):
        self.app = app
        self.prev = None
        self.prev_self = None
        self.now = {}
        self.acc = []

    def _cpu(self):
        with open("/proc/stat") as f:
            v = [int(x) for x in f.readline().split()[1:]]
        return sum(v), v[3] + v[4], v[4]

    def sample(self):
        try:
            total, idle, iowait = self._cpu()
            selfcpu = sum(os.times()[:2])
            cpu = io = sc = None
            if self.prev:
                dt = total - self.prev[0]
                if dt > 0:
                    cpu = 100.0 * (1 - (idle - self.prev[1]) / dt)
                    io = 100.0 * (iowait - self.prev[2]) / dt
            if self.prev_self is not None and self.prev:
                ncpu = os.cpu_count() or 1
                wall = (total - self.prev[0]) / ncpu / (os.sysconf("SC_CLK_TCK") or 100)
                if wall > 0:
                    sc = 100.0 * (selfcpu - self.prev_self) / wall
            self.prev = (total, idle, iowait)
            self.prev_self = selfcpu
            mem = {}
            with open("/proc/meminfo") as f:
                for l in f:
                    k, v = l.split(":", 1)
                    mem[k] = int(v.split()[0])
            mem_pct = 100.0 * (1 - mem["MemAvailable"] / mem["MemTotal"])
            swap_mb = (mem["SwapTotal"] - mem["SwapFree"]) / 1024
            du = shutil.disk_usage("/")
            self.now = {
                "ts": time.time(), "load": os.getloadavg(), "cpus": os.cpu_count(), "cpu": cpu, "iowait": io,
                "mem_pct": mem_pct, "swap_mb": swap_mb, "disk_free_mb": du.free / 1048576,
                "disk_pct": 100.0 * du.used / du.total, "sashimon_cpu": sc,
                "db_mb": self.app.store.size_bytes() / 1048576, "db_queue_dropped": self.app.store.dropped,
            }
            self.acc.append(self.now)
            minute = int(time.time() // 60)
            if len(self.acc) >= 6 or (self.acc and int(self.acc[0]["ts"] // 60) != minute):
                a = self.acc
                avg = lambda k: (sum(x[k] for x in a if x.get(k) is not None) /
                                 max(1, sum(1 for x in a if x.get(k) is not None)))
                self.app.store.put(
                    "INSERT OR REPLACE INTO host_minutes(minute, load1, cpu, iowait, mem_pct, swap_mb, disk_free_mb, "
                    "sashimon_cpu) VALUES (?,?,?,?,?,?,?,?)",
                    (int(a[0]["ts"] // 60), sum(x["load"][0] for x in a) / len(a), avg("cpu"), avg("iowait"),
                     avg("mem_pct"), avg("swap_mb"), avg("disk_free_mb"), avg("sashimon_cpu")))
                self.acc = []
        except Exception:
            traceback.print_exc()


# ---------------------------------------------------------------------------------------------------------
# Application: discovery, peers, loops
# ---------------------------------------------------------------------------------------------------------

class App:
    def __init__(self, args):
        self.args = args
        self.host = args.host_label or socket.gethostname()
        self.ring_lines = args.ring_lines
        self.freeze_s = args.freeze_seconds
        self.stall_s = args.stall_seconds
        self.split_seq = args.split_ledgers
        self.store = Store(args.db, args.max_db_mb, args.retention_hours)
        self.detector = Detector(self)
        self.metrics = HostMetrics(self)
        self.nodes = {}               # name -> Node
        self.nodes_lock = threading.Lock()
        self.peers = {p.rstrip("/"): {"url": p.rstrip("/"), "ok": False, "error": None, "data": None, "ts": None}
                      for p in args.peer}
        self.peer_ctx = ssl.create_default_context()
        self.peer_ctx.check_hostname = False
        self.peer_ctx.verify_mode = ssl.CERT_NONE
        self.started = time.time()
        self.cluster_seen = {}        # contract_id -> last time any host reported a live node for it

    # -- discovery
    def discover(self):
        found = []
        if self.args.instances_dir:
            pattern = self.args.instances_dir
            if not any(ch in pattern for ch in "*?["):
                pattern = os.path.join(pattern, "*")
            for d in sorted(glob.glob(pattern)):
                cfg = os.path.join(d, "cfg", "hp.cfg")
                if not os.path.exists(cfg):
                    continue
                try:
                    contract_id = json.load(open(cfg))["contract"]["id"]
                except Exception:
                    continue
                found.append({"name": os.path.basename(d).upper(), "dir": d, "user": None,
                              "contract_id": contract_id, "image": "local"})
        else:
            try:
                out = subprocess.run([self.args.sashi, "list"], capture_output=True, text=True, timeout=30).stdout
                for i in json.loads(out or "[]"):
                    if i.get("status") != "running":
                        continue
                    d = "/home/%s/%s" % (i["user"], i["name"])
                    found.append({"name": i["name"], "dir": d, "user": i["user"],
                                  "contract_id": i.get("contract_id", "?"), "image": i.get("image", "")})
            except Exception:
                traceback.print_exc()
                return
        names = set()
        for i in found:
            log_dir = os.path.join(i["dir"], "log")
            if not os.path.isdir(log_dir):
                continue
            names.add(i["name"])
            self.store.put("UPDATE clusters SET last_seen=? WHERE contract_id=?", (time.time(), i["contract_id"]))
            with self.nodes_lock:
                if i["name"] not in self.nodes:
                    self.nodes[i["name"]] = Node(self, i["name"], i["contract_id"], i["user"], log_dir, i["image"],
                                                 os.path.join(i["dir"], "cfg", "hp.cfg"))
        with self.nodes_lock:
            for name in list(self.nodes):
                if name not in names:
                    n = self.nodes.pop(name)
                    n.stop.set()
                    with n.lock:
                        n._flush()

    def local_nodes(self):
        with self.nodes_lock:
            return list(self.nodes.values())

    # -- federation
    def live(self):
        nodes = [n.snapshot() for n in self.local_nodes()]
        recent = {}
        for n in self.local_nodes():
            if n.contract_id not in recent and n.seq is not None:
                recent[n.contract_id] = {str(k): v for k, v in
                                         self.detector.local_recent(n.cluster_id, n.seq - 256).items()}
        return {"host": self.host, "version": VERSION, "ts": time.time(), "nodes": nodes, "recent": recent,
                "metrics": self.metrics.now}

    def poll_peers(self):
        for p in self.peers.values():
            try:
                req = urllib.request.Request(p["url"] + "/api/local/live", headers={"User-Agent": "sashimon"})
                with urllib.request.urlopen(req, timeout=8, context=self.peer_ctx) as r:
                    p["data"] = json.loads(r.read().decode())
                p["ok"], p["error"], p["ts"] = True, None, time.time()
            except Exception as e:
                p["ok"], p["error"] = False, str(e)[:200]

    def peer_get(self, url_path):
        out = []
        for p in self.peers.values():
            try:
                with urllib.request.urlopen(p["url"] + url_path, timeout=10, context=self.peer_ctx) as r:
                    out.append(json.loads(r.read().decode()))
            except Exception as e:
                out.append({"host": p["url"], "error": str(e)[:200]})
        return out

    def merged_nodes(self):
        nodes = [n.snapshot() for n in self.local_nodes()]
        for p in self.peers.values():
            d = p.get("data")
            if d and p["ts"] and time.time() - p["ts"] < PEER_STALE_S:
                age = time.time() - d["ts"]
                for n in d["nodes"]:
                    n = dict(n)
                    if n.get("idle_s") is not None:
                        n["idle_s"] = n["idle_s"] + age
                    nodes.append(n)
        return nodes

    def merged_recent(self, contract_id, min_seq):
        out = collections.defaultdict(lambda: collections.defaultdict(set))
        cid = self.store.cluster_id(contract_id)
        for seq, hashes in self.detector.local_recent(cid, min_seq).items():
            for h, ns in hashes.items():
                out[seq][h].update(ns)
        for p in self.peers.values():
            d = p.get("data")
            if d and p["ts"] and time.time() - p["ts"] < PEER_STALE_S:
                for seq, hashes in (d.get("recent") or {}).get(contract_id, {}).items():
                    if int(seq) >= min_seq:
                        for h, ns in hashes.items():
                            out[int(seq)][h].update(ns)
        return {s: {h: sorted(n) for h, n in hs.items()} for s, hs in out.items()}

    def purge_deleted(self):
        """Clusters with no live node on any host for purge_after seconds are deleted from the DB."""
        after = self.args.purge_deleted_after
        if after <= 0:
            return
        now = time.time()
        for n in self.merged_nodes():
            self.cluster_seen[n["contract_id"]] = now
        c = self.store.reader()
        try:
            rows = [dict(r) for r in c.execute("SELECT id, contract_id, last_seen FROM clusters")]
        finally:
            c.close()
        for r in rows:
            seen = max(self.cluster_seen.get(r["contract_id"], 0), r["last_seen"] or 0, self.started)
            if now - seen > after:
                print("purging deleted cluster %s (no live nodes for %ds)" % (r["contract_id"], now - seen), flush=True)
                self.detector.forget_cluster(r["id"], r["contract_id"])
                self.store.purge_cluster(r["id"])
                self.cluster_seen.pop(r["contract_id"], None)

    # -- loops
    def run_loops(self):
        def loop(fn, interval, name):
            def run():
                while True:
                    try:
                        fn()
                    except Exception:
                        traceback.print_exc()
                    time.sleep(interval)
            threading.Thread(target=run, name=name, daemon=True).start()

        loop(self.discover, 15, "discover")
        loop(self.metrics.sample, 10, "metrics")
        if self.peers:
            loop(self.poll_peers, 5, "peers")
        loop(self.detector.evaluate, 5, "detect")
        loop(lambda: [n._flush() for n in self.local_nodes()], 20, "flush")
        loop(self.store.maintain, 1800, "maintain")
        loop(self.purge_deleted, 30, "purge")


# ---------------------------------------------------------------------------------------------------------
# Queries for the API
# ---------------------------------------------------------------------------------------------------------

def q_history(app, contract_id, since, until):
    """This host's stored history for one cluster (also served to peers)."""
    c = app.store.reader()
    try:
        row = c.execute("SELECT id FROM clusters WHERE contract_id=?", (contract_id,)).fetchone()
        if not row:
            return {"host": app.host, "nodes": [], "minutes": [], "incidents": [], "ledgers": [], "stats": []}
        cid = row[0]
        nodes = [dict(r) for r in c.execute(
            "SELECT id, name, short, host, pubkey, version, image FROM nodes WHERE cluster_id=?", (cid,))]
        ids = {n["id"]: n["short"] for n in nodes}
        minutes = [dict(r, node=ids.get(r["node_id"])) for r in c.execute(
            "SELECT * FROM node_minutes WHERE minute BETWEEN ? AND ? AND node_id IN (SELECT id FROM nodes WHERE "
            "cluster_id=?) ORDER BY minute", (int(since // 60), int(until // 60), cid))]
        incidents = [dict(r, node=ids.get(r["node_id"]), host=app.host,
                          detail=json.loads(r["detail"] or "{}"),
                          captures=[x[0] for x in c.execute(
                              "SELECT kind FROM captures WHERE incident_id=?", (r["id"],))])
                     for r in c.execute(
                         "SELECT * FROM incidents WHERE cluster_id=? AND (start_ts BETWEEN ? AND ? OR end_ts IS NULL) "
                         "ORDER BY start_ts DESC LIMIT 500", (cid, since, until))]
        ledgers = [dict(r) for r in c.execute(
            "SELECT seq, hash, nodes, first_ts, last_ts FROM ledgers WHERE cluster_id=? AND last_ts BETWEEN ? AND ? "
            "ORDER BY seq DESC LIMIT 400", (cid, since, until))]
        stats = [dict(r, node=ids.get(r["node_id"]), data=json.loads(r["data"])) for r in c.execute(
            "SELECT * FROM stats WHERE ts BETWEEN ? AND ? AND node_id IN (SELECT id FROM nodes WHERE cluster_id=?) "
            "ORDER BY ts", (since, until, cid))]
        host = [dict(r) for r in c.execute(
            "SELECT * FROM host_minutes WHERE minute BETWEEN ? AND ? ORDER BY minute",
            (int(since // 60), int(until // 60)))]
        for n in nodes:
            n["host"] = app.host
        return {"host": app.host, "nodes": nodes, "minutes": minutes, "incidents": incidents, "ledgers": ledgers,
                "stats": stats, "host_minutes": host}
    finally:
        c.close()


def q_incident(app, iid):
    c = app.store.reader()
    try:
        r = c.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()
        if not r:
            return None
        names = {x[0]: x[1] for x in c.execute("SELECT id, short FROM nodes")}
        caps = []
        for cap in c.execute("SELECT node_id, ts, kind, data FROM captures WHERE incident_id=?", (iid,)):
            text = zlib.decompress(cap["data"]).decode("utf-8", "replace")
            caps.append({"node": names.get(cap["node_id"]), "ts": cap["ts"], "kind": cap["kind"],
                         "text": text if cap["kind"] == "log" else None,
                         "json": json.loads(text) if cap["kind"] == "proc" else None})
        d = dict(r)
        d["detail"] = json.loads(d["detail"] or "{}")
        d["node"] = names.get(d["node_id"])
        d["host"] = app.host
        d["captures"] = caps
        return d
    finally:
        c.close()


def q_clusters(app):
    c = app.store.reader()
    try:
        return {r["contract_id"]: dict(r) for r in c.execute("SELECT * FROM clusters")}
    finally:
        c.close()


# ---------------------------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------------------------

def make_handler(app, dashboard_path):
    class H(BaseHTTPRequestHandler):
        server_version = "sashimon/" + VERSION

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, default=str), "application/json")

        def do_GET(self):
            try:
                u = urllib.parse.urlparse(self.path)
                qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
                now = time.time()
                p = u.path
                if p in ("/", "/index.html"):
                    try:
                        with open(dashboard_path, "rb") as f:
                            return self._send(200, f.read(), "text/html; charset=utf-8")
                    except OSError:
                        return self._send(200, "<h1>sashimon %s</h1><p>dashboard.html not found next to "
                                               "sashimon.py. API is at /api/overview.</p>" % VERSION, "text/html")
                if p == "/healthz":
                    return self._json({"ok": True, "version": VERSION})
                if p == "/api/local/live":
                    return self._json(app.live())
                if p == "/api/local/history":
                    since = float(qs.get("since", now - 3600))
                    return self._json(q_history(app, qs["contract_id"], since, float(qs.get("until", now))))
                if p == "/api/local/incident":
                    return self._json(q_incident(app, int(qs["id"])) or {"error": "not found"})
                if p == "/api/overview":
                    return self._json(overview(app))
                if p == "/api/cluster":
                    window = float(qs.get("window", 3600))
                    since = now - window
                    hist = [q_history(app, qs["contract_id"], since, now)]
                    if app.peers:
                        hist += app.peer_get("/api/local/history?" + urllib.parse.urlencode(
                            {"contract_id": qs["contract_id"], "since": since, "until": now}))
                    nodes = [n for n in app.merged_nodes() if n["contract_id"] == qs["contract_id"]]
                    return self._json({"contract_id": qs["contract_id"], "since": since, "until": now,
                                       "live": nodes, "hosts": hist,
                                       "recent": app.merged_recent(qs["contract_id"],
                                                                   max([n["seq"] or 0 for n in nodes] or [0]) - 64)})
                if p == "/api/incident":
                    host, iid = qs.get("host", app.host), int(qs["id"])
                    if host == app.host:
                        return self._json(q_incident(app, iid) or {"error": "not found"})
                    for peer in app.peers.values():
                        d = peer.get("data") or {}
                        if d.get("host") == host:
                            return self._json(app_peer_one(app, peer["url"], "/api/local/incident?id=%d" % iid))
                    return self._json({"error": "unknown host"}, 404)
                return self._json({"error": "not found"}, 404)
            except Exception as e:
                traceback.print_exc()
                return self._json({"error": str(e)}, 500)

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/api/clear":
                app.store.clear()
                return self._json({"ok": True})
            return self._json({"error": "not found"}, 404)

    return H


def app_peer_one(app, base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=10, context=app.peer_ctx) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)[:200]}


def overview(app):
    now = time.time()
    nodes = app.merged_nodes()
    clusters = collections.defaultdict(list)
    for n in nodes:
        clusters[n["contract_id"]].append(n)
    known = q_clusters(app)
    out = []
    for cid, ns in clusters.items():
        seqs = [n["seq"] for n in ns if n.get("seq") is not None]
        versions = collections.Counter(n.get("version") or "?" for n in ns)
        hosts = collections.Counter(n["host"] for n in ns)
        last_close = max([n["close_ts"] for n in ns if n.get("close_ts")] or [0])
        open_inc = [{"kind": k[1], "key": k[2]} for k in app.detector.open
                    if k[0] == app.store.cluster_id(cid)]
        out.append({
            "contract_id": cid, "nodes": len(ns), "hosts": dict(hosts), "versions": dict(versions),
            "version": versions.most_common(1)[0][0], "max_seq": max(seqs) if seqs else None,
            "min_seq": min(seqs) if seqs else None, "last_close_age": now - last_close if last_close else None,
            "frozen": sum(1 for n in ns if n.get("frozen_since")),
            "silent": sum(1 for n in ns if n.get("idle_s") is None or n["idle_s"] > app.freeze_s),
            "open_incidents": open_inc, "first_seen": known.get(cid, {}).get("first_seen"),
        })
    # Clusters with no live nodes any more (e.g. an earlier test run) stay listed from the DB, so they can be compared.
    c = app.store.reader()
    try:
        for cid, rec in known.items():
            if cid in clusters or (rec.get("last_seen") or 0) < now - app.args.retention_hours * 3600:
                continue
            vers = collections.Counter(r[0] or "?" for r in c.execute(
                "SELECT version FROM nodes WHERE cluster_id=?", (rec["id"],)))
            hosts_c = collections.Counter(r[0] for r in c.execute(
                "SELECT host FROM nodes WHERE cluster_id=?", (rec["id"],)))
            if not vers:
                continue
            last = c.execute("SELECT MAX(last_ts), MAX(seq) FROM ledgers WHERE cluster_id=?", (rec["id"],)).fetchone()
            out.append({"contract_id": cid, "nodes": 0, "offline": True, "hosts": dict(hosts_c), "versions": dict(vers),
                        "version": vers.most_common(1)[0][0], "max_seq": last[1], "min_seq": None,
                        "last_close_age": now - last[0] if last[0] else None, "frozen": 0, "silent": 0,
                        "open_incidents": [], "first_seen": rec.get("first_seen"), "last_seen": rec.get("last_seen")})
    finally:
        c.close()
    hosts = [{"host": app.host, "self": True, "ok": True, "metrics": app.metrics.now, "version": VERSION,
              "nodes": len(app.local_nodes())}]
    for p in app.peers.values():
        d = p.get("data") or {}
        hosts.append({"host": d.get("host") or p["url"], "url": p["url"], "self": False, "ok": p["ok"],
                      "error": p["error"], "metrics": d.get("metrics"), "version": d.get("version"),
                      "nodes": len(d.get("nodes") or []), "age": now - p["ts"] if p["ts"] else None})
    return {"now": now, "host": app.host, "version": VERSION, "hosts": hosts,
            "clusters": sorted(out, key=lambda c: (bool(c.get("offline")), -(c["first_seen"] or 0))),
            "config": {"freeze_s": app.freeze_s, "stall_s": app.stall_s, "split_ledgers": app.split_seq,
                       "retention_h": app.args.retention_hours, "max_db_mb": app.args.max_db_mb}}


# ---------------------------------------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="HotPocket consensus observer")
    env = os.environ.get
    ap.add_argument("--db", default=env("SASHIMON_DB", "/var/lib/sashimon/sashimon.db"))
    ap.add_argument("--port", type=int, default=int(env("SASHIMON_PORT", "8765")))
    ap.add_argument("--bind", default=env("SASHIMON_BIND", "0.0.0.0"))
    ap.add_argument("--host-label", default=env("SASHIMON_HOST_LABEL"))
    ap.add_argument("--peer", action="append", default=[p for p in (env("SASHIMON_PEERS") or "").split(",") if p],
                    help="base URL of another host's sashimon (repeatable)")
    ap.add_argument("--sashi", default=env("SASHIMON_SASHI", "sashi"))
    ap.add_argument("--instances-dir", default=env("SASHIMON_INSTANCES_DIR"),
                    help="test mode: directory of node dirs (each with cfg/hp.cfg and log/hp.log) instead of sashi list")
    ap.add_argument("--retention-hours", type=float, default=float(env("SASHIMON_RETENTION_HOURS", "72")))
    ap.add_argument("--max-db-mb", type=float, default=float(env("SASHIMON_MAX_DB_MB", "512")))
    ap.add_argument("--ring-lines", type=int, default=int(env("SASHIMON_RING_LINES", "6000")))
    ap.add_argument("--freeze-seconds", type=float, default=float(env("SASHIMON_FREEZE_S", "15")))
    ap.add_argument("--stall-seconds", type=float, default=float(env("SASHIMON_STALL_S", "60")))
    ap.add_argument("--split-ledgers", type=int, default=int(env("SASHIMON_SPLIT_LEDGERS", "5")))
    ap.add_argument("--purge-deleted-after", type=float, default=float(env("SASHIMON_PURGE_DELETED_AFTER", "300")),
                    help="delete all data of a cluster once no host has seen a live node of it for this many seconds (0 = keep)")
    ap.add_argument("--dashboard", default=env("SASHIMON_DASHBOARD"))
    ap.add_argument("--tls-cert", default=env("SASHIMON_TLS_CERT"))
    ap.add_argument("--tls-key", default=env("SASHIMON_TLS_KEY"))
    ap.add_argument("--tls-auto", action="store_true", default=env("SASHIMON_TLS_AUTO") in ("1", "true"))
    args = ap.parse_args()

    app = App(args)
    app.run_loops()
    dashboard = args.dashboard or os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
    srv = ThreadingHTTPServer((args.bind, args.port), make_handler(app, dashboard))
    srv.daemon_threads = True
    cert, key = args.tls_cert, args.tls_key
    if not cert and args.tls_auto:
        base = "/etc/sashimono/contract_template/cfg"
        if os.path.exists(base + "/tlscert.pem") and os.path.exists(base + "/tlskey.pem"):
            cert, key = base + "/tlscert.pem", base + "/tlskey.pem"
    scheme = "http"
    if cert and key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        scheme = "https"
    print("sashimon %s on %s://%s:%d host=%s peers=%s db=%s" % (
        VERSION, scheme, args.bind, args.port, app.host, list(app.peers), args.db), flush=True)
    signal.signal(signal.SIGTERM, lambda *a: (srv.shutdown(), sys.exit(0)))
    srv.serve_forever()


if __name__ == "__main__":
    main()
