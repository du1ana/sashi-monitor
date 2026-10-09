<script>
  import { onMount } from 'svelte';
  import { getJSON, digest, ago, clock, clusterHealth, versionClass, short } from './util.js';
  import NodeGrid from './NodeGrid.svelte';
  import Throughput from './Throughput.svelte';
  import Heatmap from './Heatmap.svelte';
  import Incidents from './Incidents.svelte';
  import HostCharts from './HostCharts.svelte';

  let { id, overview } = $props();

  const WINDOWS = [[900, '15m'], [3600, '1h'], [6 * 3600, '6h'], [24 * 3600, '24h']];
  let win = $state(3600);
  let data = $state(null);
  let error = $state(null);
  let loading = $state(false);

  const cluster = $derived(overview.clusters.find((c) => c.contract_id === id));
  const d = $derived(data ? digest(data) : null);

  async function load() {
    loading = true;
    try {
      data = await getJSON(`api/cluster?contract_id=${encodeURIComponent(id)}&window=${win}`);
      error = null;
    } catch (e) { error = String(e.message || e); }
    loading = false;
  }

  $effect(() => { win; id; load(); });
  onMount(() => { const t = setInterval(load, 10000); return () => clearInterval(t); });
</script>

<section class="head">
  <div class="title">
    <a href="#/" class="back">← Clusters</a>
    <h2 class="mono" title={id}>{short(id, 18)}…</h2>
    {#if cluster}
      {@const h = clusterHealth(cluster)}
      <span class="pill {h.cls}"><span class="dot"></span>{h.label}</span>
      <span class="pill {versionClass(cluster.version)}">HotPocket {cluster.version}</span>
      <span class="muted">{cluster.nodes} nodes · height {cluster.max_seq ?? '—'} · last close {ago(cluster.last_close_age)} ago</span>
    {/if}
  </div>
  <div class="seg">
    {#each WINDOWS as [w, label]}
      <button class:on={win === w} onclick={() => (win = w)}>{label}</button>
    {/each}
  </div>
</section>

{#if error && !data}
  <div class="panel empty">{error}</div>
{:else if !d}
  <div class="panel empty">Loading…</div>
{:else}
  {#if d.forks.length}
    <section class="panel fork">
      <div class="panel-head"><h3>⑂ Forks: same ledger number, different hashes</h3>
        <span class="pill bad">{d.forks.length} ledger{d.forks.length > 1 ? 's' : ''}</span></div>
      <div class="panel-body">
        <table class="t">
          <thead><tr><th>Ledger</th><th>Versions</th></tr></thead>
          <tbody>
            {#each d.forks.slice(0, 20) as f}
              <tr><td class="mono num">{f.seq}</td>
                <td>{#each f.hashes as h}<div><span class="mono">{h.hash}</span> <span class="muted">· {h.nodes.join(', ')}</span></div>{/each}</td></tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>
  {/if}

  <NodeGrid live={data.live} historyNodes={d.historyNodes} stats={d.stats} />

  <section class="two">
    <Throughput series={d.series} incidents={d.incidents} since={data.since} until={data.until} />
    <section class="panel">
      <div class="panel-head"><h3>Window totals</h3><span class="faint">{WINDOWS.find((w) => w[0] === win)[1]}</span></div>
      <div class="panel-body totals">
        {#each [
          ['Ledgers closed', d.series.reduce((s, x) => s + x.closes, 0)],
          ['Vote splits', d.series.reduce((s, x) => s + x.vote_split, 0)],
          ['Too few proposals', d.series.reduce((s, x) => s + x.consensus_lost, 0)],
          ['Shard disagreement', d.series.reduce((s, x) => s + x.shard_split, 0)],
          ['Missed stages', d.series.reduce((s, x) => s + x.missed_stage, 0)],
          ['Skipped rounds', d.series.reduce((s, x) => s + x.skipped_rounds, 0)],
          ['Refused closes', d.series.reduce((s, x) => s + x.refused, 0)],
          ['Fast-forwards', d.series.reduce((s, x) => s + x.fast_forward, 0)],
          ['Late closes', d.series.reduce((s, x) => s + x.late_close, 0)],
          ['Node-seconds frozen', d.series.reduce((s, x) => s + x.frozen_s, 0)],
        ] as [k, v]}
          <div class="tot"><span class="muted">{k}</span><span class="num">{v.toLocaleString()}</span></div>
        {/each}
      </div>
    </section>
  </section>

  <Heatmap minutes={d.minutes} since={data.since} until={data.until} />
  <Incidents incidents={d.incidents} until={data.until} />
  <HostCharts hostMinutes={d.hostMinutes} since={data.since} until={data.until} />
  <p class="faint foot">Updated {clock(data.until)} UTC{loading ? ' · refreshing…' : ''}</p>
{/if}

<style>
  .head { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; }
  .title { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .back { font-size: 13px; }
  h2 { margin: 0; font-size: 17px; }
  .two { display: grid; grid-template-columns: 2fr 1fr; gap: 16px; }
  .totals { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 16px; }
  .tot { display: flex; justify-content: space-between; gap: 8px; font-size: 13px; border-bottom: 1px dashed var(--border); padding-bottom: 4px; }
  .fork { border-color: var(--bad); }
  .foot { font-size: 12px; margin: 0; }
  @media (max-width: 960px) { .two { grid-template-columns: 1fr; } }
</style>
