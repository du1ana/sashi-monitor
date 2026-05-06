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
};
