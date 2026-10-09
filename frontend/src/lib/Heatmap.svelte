<script>
  import { clock } from './util.js';
  let { minutes, since, until } = $props();

  const MODES = [
    ['closes', 'Ledgers closed'],
    ['problems', 'Problems'],
    ['vote_split', 'Vote splits'],
    ['consensus_lost', 'Too few proposals'],
    ['missed_stage', 'Missed stages'],
  ];
  let mode = $state('closes');

  const m0 = $derived(Math.floor(since / 60));
  const m1 = $derived(Math.floor(until / 60));
  const cols = $derived(Math.max(1, m1 - m0 + 1));
  const rows = $derived.by(() => {
    const by = new Map();
    for (const m of minutes) {
      const key = `${m.host}|${m.node}`;
      if (!by.has(key)) by.set(key, { host: m.host, node: m.node, cells: new Map() });
      by.get(key).cells.set(m.minute, m);
    }
    return [...by.values()].sort((a, b) => (a.host + a.node > b.host + b.node ? 1 : -1));
  });
  const maxCloses = $derived(Math.max(1, ...minutes.map((m) => m.closes || 0)));

  function cell(m) {
    if (!m) return { cls: 'none', label: 'no data' };
    const label = `${clock(m.minute * 60).slice(0, 5)} · closed ${m.closes} · height ${m.max_seq} · splits ${m.vote_split} · too few ${m.consensus_lost} · missed ${m.missed_stage} · refused ${m.refused} · ff ${m.fast_forward}${m.frozen_s ? ' · frozen ' + m.frozen_s + 's' : ''}`;
    if (m.frozen_s > 0) return { cls: 'frozen', label };
    if (mode === 'closes') {
      const f = (m.closes || 0) / maxCloses;
      return { cls: f === 0 ? 'h0' : f < 0.34 ? 'h1' : f < 0.67 ? 'h2' : 'h3', label };
    }
    if (mode === 'problems') {
      if (m.refused > 0) return { cls: 'refused', label };
      const p = (m.vote_split || 0) + (m.consensus_lost || 0) + (m.shard_split || 0);
      return { cls: p === 0 ? 'h0' : p < 5 ? 'p1' : p < 15 ? 'p2' : 'p3', label };
    }
    const v = m[mode] || 0;
    return { cls: v === 0 ? 'h0' : v < 5 ? 'p1' : v < 15 ? 'p2' : 'p3', label };
  }
</script>

<section class="panel">
  <div class="panel-head">
    <h3>Node × minute</h3>
    <div class="seg">
      {#each MODES as [k, label]}<button class:on={mode === k} onclick={() => (mode = k)}>{label}</button>{/each}
    </div>
  </div>
  <div class="panel-body">
    {#if !rows.length}
      <div class="empty">No data yet.</div>
    {:else}
      <div class="hm" style={`--cols:${cols}`}>
        {#each rows as r}
          <div class="lbl mono"><span class="faint">{r.host}</span> {r.node}</div>
          <div class="cells">
            {#each Array(cols) as _, i}
              {@const c = cell(r.cells.get(m0 + i))}
              <div class="c {c.cls}" title={c.label}></div>
            {/each}
          </div>
        {/each}
        <div></div>
        <div class="axis faint mono"><span>{clock(since).slice(0, 5)}</span><span>{clock((since + until) / 2).slice(0, 5)}</span><span>{clock(until).slice(0, 5)}</span></div>
      </div>
      <div class="key faint">
        {#if mode === 'closes'}<span><i class="c h0"></i>none</span><span><i class="c h1"></i>few</span><span><i class="c h3"></i>many</span>
        {:else}<span><i class="c h0"></i>none</span><span><i class="c p1"></i>some</span><span><i class="c p3"></i>many</span>{#if mode === 'problems'}<span><i class="c refused"></i>refused close</span>{/if}{/if}
        <span><i class="c frozen"></i>frozen</span><span><i class="c none"></i>no data</span>
      </div>
    {/if}
  </div>
</section>

<style>
  .seg button { font-size: 12px; padding: 3px 8px; }
  .hm { display: grid; grid-template-columns: 120px 1fr; gap: 3px 10px; align-items: center; }
  .lbl { font-size: 11.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .cells { display: grid; grid-template-columns: repeat(var(--cols), 1fr); gap: 1px; }
  .c { height: 16px; border-radius: 2px; background: var(--heat-0); }
  .c.none { background: transparent; outline: 1px dashed var(--border); outline-offset: -1px; }
  .c.h0 { background: var(--heat-0); } .c.h1 { background: var(--heat-1); } .c.h2 { background: var(--heat-2); } .c.h3 { background: var(--heat-3); }
  .c.p1 { background: color-mix(in srgb, var(--warn) 35%, var(--heat-0)); }
  .c.p2 { background: color-mix(in srgb, var(--warn) 70%, var(--heat-0)); }
  .c.p3 { background: var(--bad); }
  .c.refused { background: #f97316; }
  .c.frozen { background: var(--violet); }
  .axis { display: flex; justify-content: space-between; font-size: 10.5px; }
  .key { display: flex; gap: 14px; margin-top: 10px; font-size: 12px; flex-wrap: wrap; }
  .key i.c { display: inline-block; width: 12px; height: 12px; vertical-align: -2px; margin-right: 5px; }
  @media (max-width: 640px) { .hm { grid-template-columns: 70px 1fr; } .lbl .faint { display: none; } }
</style>
