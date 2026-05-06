<script>
  import { onMount, onDestroy } from 'svelte';
  import { api } from './lib/api.js';
  import { TAGS } from './lib/tags.js';
  import LineChart from './lib/LineChart.svelte';
  import InstanceCard from './lib/InstanceCard.svelte';
  import TagFilter from './lib/TagFilter.svelte';
  import WindowPicker from './lib/WindowPicker.svelte';
  import EventDrawer from './lib/EventDrawer.svelte';
  import { fmtNum, fmtTime } from './lib/format.js';

  let windowVal = $state('3600');
  let refreshMs = $state(5000);
  let summary = $state([]);
  let globalBuckets = $state([]);
  let perInstanceBuckets = $state({});  // name -> [bucket]
  let visible = $state(Object.fromEntries(TAGS.map(t => [t.id, true])));
  let lastUpdate = $state(null);
  let loading = $state(true);
  let err = $state('');
  let selected = $state(null);
  let timer;

  function bucketSecFor(w) {
    if (w === 'all') return 3600;
    const n = +w;
    if (n <= 900)   return 30;
    if (n <= 3600)  return 60;
    if (n <= 21600) return 300;
    if (n <= 86400) return 900;
    return 3600;
  }

  let bucketSec = $derived(bucketSecFor(windowVal));

  async function refresh() {
    try {
      const w = windowVal;
      const [s, gb] = await Promise.all([
        api.summary(w),
        api.histogram(w, bucketSec),
      ]);
      summary = s;
      globalBuckets = gb;

      // Per-instance histograms
      const next = {};
      await Promise.all(s.map(async (inst) => {
        next[inst.name] = await api.histogram(w, bucketSec, inst.name);
      }));
      perInstanceBuckets = next;

      lastUpdate = Date.now();
      err = '';
    } catch (e) {
      err = String(e.message || e);
    } finally {
      loading = false;
    }
  }

  function reschedule() {
    clearInterval(timer);
    if (refreshMs > 0) timer = setInterval(refresh, refreshMs);
  }

  // Aggregate counts across all instances (for TagFilter chips).
  let aggCounts = $derived.by(() => {
    const out = {};
    for (const inst of summary) {
      for (const [k, v] of Object.entries(inst.counts || {})) {
        out[k] = (out[k] || 0) + v;
      }
    }
    return out;
  });

  // Aggregate health counts.
  let healthCounts = $derived.by(() => {
    const out = { healthy: 0, consensus_loss: 0, forked: 0, stalled: 0, unknown: 0 };
    for (const inst of summary) out[inst.health] = (out[inst.health] || 0) + 1;
    return out;
  });

  let totalLedgers = $derived(aggCounts.ledger_created || 0);
  let totalErrors  = $derived((aggCounts.error || 0) + (aggCounts.fork_warn || 0) + (aggCounts.consensus_lost || 0));

  onMount(() => {
    refresh();
    reschedule();
  });
  onDestroy(() => clearInterval(timer));

  // Re-run when window changes.
  $effect(() => { void windowVal; refresh(); });
  $effect(() => { void refreshMs; reschedule(); });
</script>

<div class="app">
  <header class="hdr">
    <div class="brand">
      <svg width="22" height="22" viewBox="0 0 64 64" aria-hidden="true">
        <rect width="64" height="64" rx="14" fill="var(--green)"/>
        <path d="M14 38l8-12 8 8 12-18 8 18" stroke="#06120e" stroke-width="5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <span class="name">sashimon</span>
      <span class="tag dim">Sashimono HotPocket monitor</span>
    </div>
    <div class="hdr-meta dim">
      {#if lastUpdate}updated {fmtTime(lastUpdate / 1000)}{:else}—{/if}
      {#if err}<span class="err-pill" title={err}>error</span>{/if}
    </div>
  </header>

  <section class="kpis">
    <div class="kpi">
      <span class="k">Instances</span>
      <span class="v">{summary.length}</span>
      <div class="health-row">
        {#each Object.entries(healthCounts) as [k, c]}
          {#if c > 0}
            <span class="hbit hbit-{k}">{c}</span>
          {/if}
        {/each}
      </div>
    </div>
    <div class="kpi">
      <span class="k">Ledgers <span class="muted">({windowVal === 'all' ? 'all-time' : 'window'})</span></span>
      <span class="v">{fmtNum(totalLedgers)}</span>
    </div>
    <div class="kpi">
      <span class="k">Issues <span class="muted">(errors + forks + cons-loss)</span></span>
      <span class="v" class:bad={totalErrors > 0}>{fmtNum(totalErrors)}</span>
    </div>
    <div class="kpi window">
      <span class="k">Window</span>
      <WindowPicker bind:value={windowVal} />
    </div>
    <div class="kpi window">
      <span class="k">Refresh</span>
      <select bind:value={refreshMs} class="select">
        <option value={0}>off</option>
        <option value={5000}>5s</option>
        <option value={15000}>15s</option>
        <option value={60000}>60s</option>
      </select>
    </div>
  </section>

  <section class="filters">
    <span class="filters-label dim">Filter</span>
    <TagFilter bind:visible counts={aggCounts} />
  </section>

  <section class="panel">
    <div class="panel-head">
      <h2>All instances · events / {bucketSec >= 86400 ? 'day' : bucketSec >= 3600 ? 'hour' : bucketSec >= 60 ? 'min' : 'tick'}</h2>
      <span class="dim">{globalBuckets.length} buckets · {bucketSec}s</span>
    </div>
    <LineChart buckets={globalBuckets} {bucketSec} tags={TAGS} {visible} height={260} showLegend={true} />
  </section>

  <section class="grid">
    {#if loading && summary.length === 0}
      {#each Array(4) as _}
        <div class="card-skel">
          <div class="skel" style="height:14px;width:60%"></div>
          <div class="skel" style="height:10px;width:40%;margin-top:10px"></div>
          <div class="skel" style="height:130px;margin-top:14px"></div>
        </div>
      {/each}
    {:else if summary.length === 0}
      <div class="empty">
        <h3>No Sashimono instances detected yet.</h3>
        <p class="dim">Confirm <code>sashi list</code> works on this VM. The daemon polls every 30 seconds.</p>
      </div>
    {:else}
      {#each summary as inst (inst.name)}
        <InstanceCard
          instance={inst}
          buckets={perInstanceBuckets[inst.name] || []}
          {bucketSec}
          {visible}
          onSelect={(i) => selected = i}
        />
      {/each}
    {/if}
  </section>

  <footer class="ftr dim">
    <a href="https://github.com/du1ana/sashi-monitor" target="_blank" rel="noopener">github.com/du1ana/sashi-monitor</a>
    <span class="sep"></span>
    <span>API: <code>/api/summary</code>, <code>/api/histogram</code>, <code>/api/events</code></span>
  </footer>
</div>

{#if selected}
  <EventDrawer instance={selected} onClose={() => selected = null} />
{/if}

<style>
  .app {
    max-width: 1480px;
    margin: 0 auto;
    padding: 20px clamp(16px, 3vw, 32px) 60px;
  }

  /* ---- header ---- */
  .hdr {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 0 22px;
    gap: 16px; flex-wrap: wrap;
  }
  .brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
  .name {
    font-size: 18px; font-weight: 700; letter-spacing: -0.01em;
  }
  .tag { font-size: 12px; }
  .hdr-meta { font-size: 11px; display: flex; align-items: center; gap: 10px; }
  .err-pill {
    background: rgba(255,93,108,0.18);
    color: #ff97a2;
    font-size: 10px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.08em;
    padding: 2px 8px; border-radius: 999px;
  }

  /* ---- KPIs ---- */
  .kpis {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 12px;
    margin-bottom: 18px;
  }
  .kpi {
    background: linear-gradient(180deg, var(--bg-1), var(--bg-2));
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 14px 16px;
    display: flex; flex-direction: column; gap: 6px;
    min-width: 0;
    box-shadow: var(--shadow-1);
  }
  .kpi.window { gap: 10px; }
  .k {
    font-size: 10px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--fg-dim);
  }
  .k .muted { color: var(--fg-dim); font-weight: 500; text-transform: none; letter-spacing: 0; }
  .v {
    font-size: 24px; font-weight: 700;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.02em;
  }
  .v.bad { color: var(--red); }
  .health-row { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 2px; }
  .hbit {
    font-size: 10px; font-weight: 700;
    padding: 1px 7px; border-radius: 999px;
    font-variant-numeric: tabular-nums;
  }
  .hbit-healthy        { background: rgba(0,214,143,.16);  color: #69f0bd; }
  .hbit-consensus_loss { background: rgba(245,177,74,.16); color: #ffd28a; }
  .hbit-forked         { background: rgba(255,93,108,.18); color: #ff97a2; }
  .hbit-stalled        { background: rgba(177,140,255,.18);color: #d4baff; }
  .hbit-unknown        { background: rgba(91,102,122,.25); color: #aab4c2; }

  .select {
    background: var(--bg-2);
    color: var(--fg);
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 6px 10px;
    font-size: 12px; font-weight: 600;
    width: max-content;
  }

  /* ---- filters ---- */
  .filters {
    display: flex; align-items: center; gap: 10px;
    margin-bottom: 18px;
    flex-wrap: wrap;
  }
  .filters-label {
    font-size: 10px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.08em;
  }

  /* ---- main panel ---- */
  .panel {
    background: linear-gradient(180deg, var(--bg-1), var(--bg-2));
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 18px;
    margin-bottom: 22px;
    box-shadow: var(--shadow-1);
  }
  .panel-head {
    display: flex; align-items: baseline; justify-content: space-between;
    margin-bottom: 8px;
    gap: 12px; flex-wrap: wrap;
  }
  .panel-head h2 {
    margin: 0; font-size: 12px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.08em; color: var(--fg-muted);
  }
  .panel-head .dim { font-size: 10.5px; }

  /* ---- instance grid ---- */
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
    gap: 14px;
  }
  .card-skel {
    background: var(--bg-1);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 16px;
  }
  .empty {
    grid-column: 1 / -1;
    text-align: center;
    padding: 60px 20px;
    border: 1px dashed var(--line);
    border-radius: var(--radius);
  }
  .empty h3 { margin: 0 0 6px; font-size: 14px; }
  .empty code {
    background: var(--bg-3); padding: 2px 6px; border-radius: 4px;
    font-family: var(--mono); font-size: 12px;
  }

  /* ---- footer ---- */
  .ftr {
    margin-top: 32px;
    padding: 16px 0;
    border-top: 1px solid var(--line);
    font-size: 11px;
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
  }
  .ftr a { color: var(--fg-muted); text-decoration: none; }
  .ftr a:hover { color: var(--fg); }
  .ftr .sep { width: 4px; height: 4px; background: var(--line-2); border-radius: 50%; }
  .ftr code { font-family: var(--mono); font-size: 10.5px; color: var(--fg-muted); }

  /* ---- responsive ---- */
  @media (max-width: 1100px) {
    .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .kpi.window:last-child { grid-column: span 2; }
  }
  @media (max-width: 600px) {
    .kpis { grid-template-columns: 1fr; }
    .kpi.window:last-child { grid-column: 1; }
    .panel { padding: 14px; }
    .v { font-size: 20px; }
  }
</style>
