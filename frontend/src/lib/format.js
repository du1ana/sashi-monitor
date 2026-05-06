export function fmtAge(s) {
  if (s == null) return '—';
  if (s < 1)     return '<1s';
  if (s < 60)    return s.toFixed(0) + 's';
  if (s < 3600)  return (s/60).toFixed(s < 600 ? 1 : 0) + 'm';
  if (s < 86400) return (s/3600).toFixed(s < 36000 ? 1 : 0) + 'h';
  return (s/86400).toFixed(1) + 'd';
}

export function fmtNum(n) {
  if (n == null) return '0';
  if (n < 1000) return String(n);
  if (n < 10000) return (n/1000).toFixed(1) + 'k';
  if (n < 1e6)   return Math.round(n/1000) + 'k';
  return (n/1e6).toFixed(1) + 'M';
}

export function fmtTime(ts, withDate = false) {
  const d = new Date(ts * 1000);
  const t = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  if (!withDate) return t;
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + t;
}

export function bucketLabel(ts, bucketSec) {
  const d = new Date(ts * 1000);
  if (bucketSec >= 86400) {
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  }
  if (bucketSec >= 3600) {
    return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', hour12: false });
  }
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
}

export function shortName(name, n = 12) {
  if (!name) return '';
  if (name.length <= n) return name;
  return name.slice(0, n);
}
