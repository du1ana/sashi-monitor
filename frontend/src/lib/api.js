async function j(path) {
  const r = await fetch(path, { cache: 'no-store' });
  if (!r.ok) throw new Error(path + ' ' + r.status);
  return r.json();
}

export const api = {
  instances: () => j('/api/instances'),
  summary:   (window) => j(`/api/summary?window=${window}`),
  histogram: (window, bucket, instance) => {
    const q = new URLSearchParams({ window, bucket });
    if (instance) q.set('instance', instance);
    return j('/api/histogram?' + q.toString());
  },
  events: ({ instance, since, until, tag, limit = 500 } = {}) => {
    const q = new URLSearchParams({ limit });
    if (instance) q.set('instance', instance);
    if (since != null) q.set('since', since);
    if (until != null) q.set('until', until);
    if (tag) q.set('tag', tag);
    return j('/api/events?' + q.toString());
  },
  spells: ({ instance, window = '3600', tags, maxGap, minCount } = {}) => {
    const q = new URLSearchParams({ window });
    if (instance) q.set('instance', instance);
    if (tags && tags.length) q.set('tags', tags.join(','));
    if (maxGap != null) q.set('max_gap', maxGap);
    if (minCount != null) q.set('min_count', minCount);
    return j('/api/spells?' + q.toString());
  },
  clearAll: async () => {
    const r = await fetch('/api/clear', { method: 'POST', cache: 'no-store' });
    if (!r.ok) throw new Error('/api/clear ' + r.status);
    return r.json();
  },
};
