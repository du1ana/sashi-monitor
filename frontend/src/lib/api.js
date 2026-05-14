async function j(path) {
  const r = await fetch(path, { cache: 'no-store' });
  if (!r.ok) throw new Error(path + ' ' + r.status);
  return r.json();
}

export const api = {
  instances: () => j('/api/instances'),
  summary:   (window, contractId) => {
    const q = new URLSearchParams({ window });
    if (contractId) q.set('contract_id', contractId);
    return j('/api/summary?' + q.toString());
  },
  histogram: (window, bucket, instance, contractId) => {
    const q = new URLSearchParams({ window, bucket });
    if (instance) q.set('instance', instance);
    if (contractId) q.set('contract_id', contractId);
    return j('/api/histogram?' + q.toString());
  },
  events: ({ instance, since, until, tag, limit = 500, contractId } = {}) => {
    const q = new URLSearchParams({ limit });
    if (instance) q.set('instance', instance);
    if (since != null) q.set('since', since);
    if (until != null) q.set('until', until);
    if (tag) q.set('tag', tag);
    if (contractId) q.set('contract_id', contractId);
    return j('/api/events?' + q.toString());
  },
  spells: ({ instance, window = '3600', tags, maxGap, minCount, contractId } = {}) => {
    const q = new URLSearchParams({ window });
    if (instance) q.set('instance', instance);
    if (tags && tags.length) q.set('tags', tags.join(','));
    if (maxGap != null) q.set('max_gap', maxGap);
    if (minCount != null) q.set('min_count', minCount);
    if (contractId) q.set('contract_id', contractId);
    return j('/api/spells?' + q.toString());
  },
  clusters: () => j('/api/clusters'),
  setClusterMonitored: async (contractIds, monitored) => {
    const body = Array.isArray(contractIds)
      ? { contract_ids: contractIds, monitored }
      : { contract_id: contractIds, monitored };
    const r = await fetch('/api/clusters/monitor', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      cache: 'no-store',
    });
    if (!r.ok) throw new Error('/api/clusters/monitor ' + r.status);
    return r.json();
  },
  discoverNow: async () => {
    const r = await fetch('/api/discover_now', { method: 'POST', cache: 'no-store' });
    if (!r.ok) throw new Error('/api/discover_now ' + r.status);
    return r.json();
  },
  clearAll: async () => {
    const r = await fetch('/api/clear', { method: 'POST', cache: 'no-store' });
    if (!r.ok) throw new Error('/api/clear ' + r.status);
    return r.json();
  },
  policy:    () => j('/api/policy'),
  setPolicy: async (mode) => {
    const r = await fetch('/api/policy', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
      cache: 'no-store',
    });
    if (!r.ok) throw new Error('/api/policy ' + r.status);
    return r.json();
  },
  dbSize:    () => j('/api/db_size'),
};
