<script>
  import { clock } from './util.js';
  let { hostMinutes, since, until } = $props();

  const SERIES = [
    ['load1', 'Load (1m)', null],
    ['cpu', 'CPU %', 100],
    ['iowait', 'IO wait %', 100],
    ['mem_pct', 'Memory %', 100],
  ];
  const W = 300, H = 70;
  const x = (ts) => ((ts - since) / Math.max(1, until - since)) * W;

  function line(rows, key, max) {
    const mx = max ?? Math.max(1, ...rows.map((r) => r[key] || 0));
    const pts = rows.filter((r) => r[key] != null)
      .map((r) => `${x(r.minute * 60 + 30).toFixed(1)},${(H - ((r[key] || 0) / mx) * (H - 6) - 2).toFixed(1)}`);
    return { d: pts.length ? 'M' + pts.join('L') : '', max: mx };
  }
  const last = (rows, key) => { const r = rows.filter((x) => x[key] != null).at(-1); return r ? r[key] : null; };
</script>

<section class="panel">
  <div class="panel-head"><h3>Host load</h3><span class="faint">per minute, {clock(since).slice(0, 5)}–{clock(until).slice(0, 5)} UTC</span></div>
  <div class="panel-body grid4">
    {#each Object.entries(hostMinutes) as [host, rows]}
      {#each SERIES as [key, label, max]}
        {@const l = line(rows, key, max)}
        <div class="mini">
          <div class="top"><span class="muted">{host} · {label}</span><b class="num">{last(rows, key)?.toFixed(key === 'load1' ? 1 : 0) ?? '—'}</b></div>
          <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
            <path d={l.d} class="ln" />
          </svg>
        </div>
      {/each}
    {/each}
  </div>
</section>

<style>
  .grid4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px 18px; }
  .mini .top { display: flex; justify-content: space-between; font-size: 12px; }
  svg { width: 100%; height: 56px; display: block; background: var(--panel-2); border-radius: 6px; margin-top: 4px; }
  .ln { fill: none; stroke: var(--accent); stroke-width: 1.6; vector-effect: non-scaling-stroke; }
  @media (max-width: 960px) { .grid4 { grid-template-columns: repeat(2, 1fr); } }
</style>
