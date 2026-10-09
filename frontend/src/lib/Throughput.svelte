<script>
  import { incMeta, clock } from './util.js';
  let { series, incidents, since, until } = $props();

  const W = 900, H = 220, P = { l: 36, r: 12, t: 12, b: 26 };
  const x = (ts) => P.l + ((ts - since) / Math.max(1, until - since)) * (W - P.l - P.r);
  const yMax = $derived(Math.max(10, ...series.map((s) => Math.max(s.closes, s.vote_split / 5))));
  const y = (v) => H - P.b - (v / yMax) * (H - P.t - P.b);
  const path = (key, scale = 1) =>
    series.map((s, i) => `${i ? 'L' : 'M'}${x(s.minute * 60 + 30).toFixed(1)},${y((s[key] || 0) / scale).toFixed(1)}`).join('');
  const area = $derived(series.length
    ? `${path('closes')}L${x(series.at(-1).minute * 60 + 30).toFixed(1)},${H - P.b}L${x(series[0].minute * 60 + 30).toFixed(1)},${H - P.b}Z` : '');
  const marks = $derived(incidents.filter((i) => ['fork', 'stall', 'height_split', 'freeze'].includes(i.kind) && i.start_ts >= since));
  const ticks = $derived(Array.from({ length: 6 }, (_, i) => since + ((until - since) * i) / 5));
  let hover = $state(null);

  function onMove(e) {
    const r = e.currentTarget.getBoundingClientRect();
    const ts = since + ((e.clientX - r.left) / r.width * W - P.l) / (W - P.l - P.r) * (until - since);
    const m = Math.floor(ts / 60);
    hover = series.find((s) => s.minute === m) || null;
  }
</script>

<section class="panel">
  <div class="panel-head">
    <h3>Ledgers closed per minute</h3>
    <div class="legend">
      <span><i class="sw ok"></i>closes</span><span><i class="sw warn"></i>vote splits ÷5</span>
      <span><i class="sw bar"></i>stalls / splits / freezes</span>
    </div>
  </div>
  <div class="panel-body">
    {#if !series.length}
      <div class="empty">No data in this window yet.</div>
    {:else}
      <div class="wrap">
        <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label="Ledgers closed per minute"
             onmousemove={onMove} onmouseleave={() => (hover = null)}>
          {#each [0, 0.25, 0.5, 0.75, 1] as f}
            <line x1={P.l} x2={W - P.r} y1={y(yMax * f)} y2={y(yMax * f)} class="grid" />
            <text x={P.l - 6} y={y(yMax * f) + 4} class="axis" text-anchor="end">{Math.round(yMax * f)}</text>
          {/each}
          {#each marks as m}
            <rect x={x(m.start_ts)} width={Math.max(2, x(m.end_ts ?? until) - x(m.start_ts))} y={P.t} height={H - P.t - P.b}
                  class="mark {incMeta(m.kind).cls}"><title>{incMeta(m.kind).label}: {m.summary}</title></rect>
          {/each}
          <path d={area} class="area" />
          <path d={path('closes')} class="line ok" />
          <path d={path('vote_split', 5)} class="line warn" />
          {#each ticks as t}
            <text x={x(t)} y={H - 6} class="axis" text-anchor="middle">{clock(t).slice(0, 5)}</text>
          {/each}
        </svg>
        {#if hover}
          <div class="tip">
            <b>{clock(hover.minute * 60).slice(0, 5)}</b> · height {hover.max_seq} · {hover.closes} closed ·
            {hover.vote_split} vote splits · {hover.consensus_lost} too few · {hover.refused} refused · {hover.fast_forward} ff
          </div>
        {/if}
      </div>
    {/if}
  </div>
</section>

<style>
  svg { width: 100%; height: 220px; display: block; }
  .grid { stroke: var(--border); stroke-width: 1; }
  .axis { fill: var(--faint); font-size: 10px; font-family: var(--mono); }
  .area { fill: var(--ok); opacity: .12; }
  .line { fill: none; stroke-width: 1.8; vector-effect: non-scaling-stroke; }
  .line.ok { stroke: var(--ok); } .line.warn { stroke: var(--warn); }
  .mark { opacity: .16; } .mark.bad { fill: var(--bad); } .mark.warn { fill: var(--warn); } .mark.violet { fill: var(--violet); }
  .legend { display: flex; gap: 12px; font-size: 12px; color: var(--muted); flex-wrap: wrap; }
  .sw { display: inline-block; width: 10px; height: 3px; border-radius: 2px; vertical-align: middle; margin-right: 5px; }
  .sw.ok { background: var(--ok); } .sw.warn { background: var(--warn); } .sw.bar { background: var(--bad); opacity: .35; height: 10px; }
  .wrap { position: relative; }
  .tip { margin-top: 6px; font-size: 12.5px; color: var(--muted); }
</style>
