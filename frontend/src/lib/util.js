// Data helpers shared by the views.

export async function getJSON(path) {
  const r = await fetch(path, { cache: 'no-store' });
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

export const short = (id, n = 8) => (id ? String(id).slice(0, n) : '?');

export function ago(sec) {
  if (sec == null || !isFinite(sec)) return '—';
  sec = Math.max(0, sec);
  if (sec < 60) return `${Math.round(sec)}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${Math.round(sec % 60)}s`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
  return `${Math.floor(sec / 86400)}d ${Math.floor((sec % 86400) / 3600)}h`;
}

export function clock(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toISOString().slice(11, 19);
}

export function dur(start, end, now) {
  const e = end ?? now;
  return ago(e - start);
}

export function versionClass(v) {
  if (!v || v === '?') return 'neutral';
  if (v.startsWith('0.6.5') || v.startsWith('0.6.4')) return 'v065';
  return 'v066';
}

// Overall state of a cluster from the overview record.
export function clusterHealth(c) {
  if (c.offline) return { label: 'Offline', cls: 'neutral' };
  const kinds = new Set((c.open_incidents || []).map((i) => i.kind));
  if (kinds.has('fork')) return { label: 'Forked', cls: 'bad' };
  if (kinds.has('stall') || (c.last_close_age != null && c.last_close_age > 60)) return { label: 'Stalled', cls: 'bad' };
  if (kinds.has('height_split') || kinds.has('freeze') || c.frozen > 0) return { label: 'Degraded', cls: 'warn' };
  if (c.silent > 0) return { label: 'Degraded', cls: 'warn' };
  return { label: 'Healthy', cls: 'ok' };
}

export const INCIDENT_META = {
  fork: { label: 'Fork', cls: 'bad', icon: '⑂' },
  stall: { label: 'Stall', cls: 'bad', icon: '■' },
  height_split: { label: 'Height split', cls: 'warn', icon: '⇵' },
  freeze: { label: 'Node frozen', cls: 'violet', icon: '❄' },
  refused_close: { label: 'Refused close', cls: 'warn', icon: '⛔' },
  fast_forward: { label: 'Fast-forward', cls: 'info', icon: '⏩' },
  config_patch: { label: 'Config / UNL', cls: 'info', icon: '⚙' },
  patch_after_sync: { label: 'Patch after sync', cls: 'info', icon: '⚙' },
  invalid_cert: { label: 'Invalid cert', cls: 'bad', icon: '!' },
  conflicting_cert: { label: 'Conflicting cert', cls: 'bad', icon: '!!' },
  node_start: { label: 'Node start', cls: 'neutral', icon: '▶' },
  error: { label: 'Error', cls: 'warn', icon: '!' },
};
export const incMeta = (k) => INCIDENT_META[k] || { label: k, cls: 'neutral', icon: '•' };

// Flatten a /api/cluster response into what the views need.
export function digest(data) {
  const hosts = (data.hosts || []).filter((h) => !h.error);
  const minutes = hosts.flatMap((h) => (h.minutes || []).map((m) => ({ ...m, host: h.host })));
  const raw = hosts
    .flatMap((h) => (h.incidents || []).map((i) => ({ ...i, host: i.host || h.host })))
    .sort((a, b) => a.start_ts - b.start_ts);
  // Cluster-wide incidents (stall, split, fork) are detected independently by every host: merge them.
  const incidents = [];
  for (const i of raw) {
    if (!i.node && ['stall', 'height_split', 'fork'].includes(i.kind)) {
      const twin = incidents.find((x) => !x.node && x.kind === i.kind && Math.abs(x.start_ts - i.start_ts) < 45 &&
        (i.kind !== 'fork' || x.detail?.seq === i.detail?.seq));
      if (twin) {
        twin.hosts = [...new Set([...(twin.hosts || [twin.host]), i.host])];
        twin.others = [...(twin.others || []), i];
        if (twin.end_ts != null && (i.end_ts == null || i.end_ts > twin.end_ts)) twin.end_ts = i.end_ts;
        continue;
      }
    }
    incidents.push({ ...i, hosts: [i.host] });
  }
  incidents.sort((a, b) => b.start_ts - a.start_ts);
  const ledgers = hosts.flatMap((h) => h.ledgers || []);
  const stats = hosts.flatMap((h) => h.stats || []);
  const hostMinutes = Object.fromEntries(hosts.map((h) => [h.host, h.host_minutes || []]));
  const historyNodes = hosts.flatMap((h) => h.nodes || []);

  // Forks visible in stored ledgers (same seq, different hash), merged across hosts.
  const bySeq = new Map();
  for (const l of ledgers) {
    const m = bySeq.get(l.seq) || new Map();
    const nodes = new Set([...(m.get(l.hash) || []), ...String(l.nodes || '').split(',').filter(Boolean)]);
    m.set(l.hash, nodes);
    bySeq.set(l.seq, m);
  }
  for (const [seq, hs] of Object.entries(data.recent || {})) {
    const m = bySeq.get(+seq) || new Map();
    for (const [h, ns] of Object.entries(hs)) m.set(h, new Set([...(m.get(h) || []), ...ns]));
    bySeq.set(+seq, m);
  }
  const forks = [...bySeq.entries()]
    .filter(([, m]) => m.size > 1)
    .map(([seq, m]) => ({ seq, hashes: [...m.entries()].map(([h, ns]) => ({ hash: h, nodes: [...ns].sort() })) }))
    .sort((a, b) => b.seq - a.seq);

  // Per-minute cluster series.
  const byMinute = new Map();
  for (const m of minutes) {
    const r = byMinute.get(m.minute) || { minute: m.minute, max_seq: 0, closes: 0, vote_split: 0, consensus_lost: 0,
      shard_split: 0, refused: 0, fast_forward: 0, frozen_s: 0, missed_stage: 0, skipped_rounds: 0, late_close: 0,
      nodes: 0 };
    r.max_seq = Math.max(r.max_seq, m.max_seq || 0);
    r.closes = Math.max(r.closes, m.closes || 0);
    for (const k of ['vote_split', 'consensus_lost', 'shard_split', 'refused', 'fast_forward', 'frozen_s',
      'missed_stage', 'skipped_rounds', 'late_close']) r[k] += m[k] || 0;
    r.nodes += 1;
    byMinute.set(m.minute, r);
  }
  const series = [...byMinute.values()].sort((a, b) => a.minute - b.minute);

  return { hosts, minutes, incidents, ledgers, stats, hostMinutes, historyNodes, forks, series };
}

// Summary numbers for one cluster over the loaded window (used by the compare view).
export function metrics(data) {
  const d = digest(data);
  const span = Math.max(60, (data.until - data.since));
  const mins = d.series.length || 1;
  const count = (k) => d.incidents.filter((i) => i.kind === k).length;
  const stallSecs = d.incidents.filter((i) => i.kind === 'stall')
    .reduce((s, i) => s + ((i.end_ts ?? data.until) - Math.max(i.start_ts, data.since)), 0);
  const nodeCount = new Set(d.minutes.map((m) => m.node + '@' + m.host)).size || 1;
  const sum = (k) => d.minutes.reduce((s, m) => s + (m[k] || 0), 0);
  const zeroMinutes = d.series.filter((s) => (s.closes || 0) === 0).length;
  return {
    ledgersPerMin: d.series.reduce((s, x) => s + (x.closes || 0), 0) / mins,
    zeroMinutesPct: (100 * zeroMinutes) / mins,
    stallPct: (100 * stallSecs) / span,
    forks: Math.max(count('fork'), d.forks.length),
    stalls: count('stall'),
    splits: count('height_split'),
    freezes: count('freeze'),
    refused: sum('refused'),
    fastForwards: sum('fast_forward'),
    voteSplitsPerNodeMin: sum('vote_split') / nodeCount / mins,
    consensusLostPerNodeMin: sum('consensus_lost') / nodeCount / mins,
    missedStagesPerNodeMin: sum('missed_stage') / nodeCount / mins,
    skippedPerNodeMin: sum('skipped_rounds') / nodeCount / mins,
  };
}
