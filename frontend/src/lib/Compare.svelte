<script>
  import { onMount } from 'svelte';
  import { getJSON, metrics, versionClass, short, clusterHealth } from './util.js';
  import Throughput from './Throughput.svelte';
  import { digest } from './util.js';

  let { a, b, overview } = $props();
  const WINDOWS = [[3600, '1h'], [6 * 3600, '6h'], [24 * 3600, '24h']];
  let win = $state(3600);
  let da = $state(null), db = $state(null), error = $state(null);

  async function load() {
    try {
      [da, db] = await Promise.all([a, b].map((id) =>
        getJSON(`api/cluster?contract_id=${encodeURIComponent(id)}&window=${win}`)));
      error = null;
    } catch (e) { error = String(e.message || e); }
  }
  $effect(() => { win; a; b; load(); });
  onMount(() => { const t = setInterval(load, 15000); return () => clearInterval(t); });

  const ca = $derived(overview.clusters.find((c) => c.contract_id === a));
  const cb = $derived(overview.clusters.find((c) => c.contract_id === b));
  const ma = $derived(da ? metrics(da) : null);
  const mb = $derived(db ? metrics(db) : null);

  // [key, label, better: 'high' | 'low', format]
  const ROWS = [
    ['ledgersPerMin', 'Ledgers closed / minute', 'high', (v) => v.toFixed(1)],
    ['zeroMinutesPct', 'Minutes with no ledger', 'low', (v) => v.toFixed(0) + '%'],
    ['stallPct', 'Time stalled', 'low', (v) => v.toFixed(1) + '%'],
    ['forks', 'Forks (ledgers with >1 hash)', 'low', (v) => v],
    ['stalls', 'Stall incidents', 'low', (v) => v],
    ['splits', 'Height-split incidents', 'low', (v) => v],
    ['freezes', 'Node freezes', 'low', (v) => v],
    ['refused', 'Refused closes (forks avoided)', null, (v) => v, true],
    ['fastForwards', 'Certified fast-forwards', null, (v) => v, true],
    ['voteSplitsPerNodeMin', 'Vote splits / node / min', 'low', (v) => v.toFixed(2)],
    ['consensusLostPerNodeMin', '"Not enough peers" / node / min', 'low', (v) => v.toFixed(2)],
    ['missedStagesPerNodeMin', 'Missed stages / node / min', 'low', (v) => v.toFixed(2)],
    ['skippedPerNodeMin', 'Skipped rounds / node / min', 'low', (v) => v.toFixed(2), true],
  ];
  // Rows marked true rely on log lines that only 0.6.6+ writes; for older builds the value is unknown, not zero.
  const known = (c) => !!c && !/^0\.6\.[0-5]$/.test(c.version || '');
  function winner(key, better) {
    if (!better || !ma || !mb || ma[key] === mb[key]) return null;
    return (better === 'high') === (ma[key] > mb[key]) ? 'a' : 'b';
  }
</script>

<section class="head">
  <div class="title"><a href="#/" class="back">← Clusters</a><h2>Compare</h2></div>
  <div class="seg">{#each WINDOWS as [w, l]}<button class:on={win === w} onclick={() => (win = w)}>{l}</button>{/each}</div>
</section>

{#if error}<div class="panel empty">{error}</div>{/if}

<section class="panel">
  <table class="t cmp">
    <thead>
      <tr>
        <th>Metric</th>
        {#each [[a, ca], [b, cb]] as [id, c]}
          <th>
            <a href={`#/c/${encodeURIComponent(id)}`} class="mono">{short(id, 12)}…</a>
            {#if c}
              <div class="hd"><span class="pill {versionClass(c.version)}">HotPocket {c.version}</span>
                <span class="pill {clusterHealth(c).cls}">{clusterHealth(c).label}</span>
                <span class="faint">{c.nodes} nodes</span></div>
            {/if}
          </th>
        {/each}
      </tr>
    </thead>
    <tbody>
      {#if ma && mb}
        {#each ROWS as [key, label, better, fmt, newOnly]}
          {@const okA = !newOnly || known(ca)}
          {@const okB = !newOnly || known(cb)}
          {@const w = okA && okB ? winner(key, better) : null}
          <tr><td>{label}{#if newOnly}<span class="faint"> · 0.6.6+</span>{/if}</td>
            <td class="num" class:win={w === 'a'} class:lose={w === 'b'}>{okA ? fmt(ma[key]) : 'n/a'}</td>
            <td class="num" class:win={w === 'b'} class:lose={w === 'a'}>{okB ? fmt(mb[key]) : 'n/a'}</td></tr>
        {/each}
      {:else}
        <tr><td colspan="3" class="empty">Loading…</td></tr>
      {/if}
    </tbody>
  </table>
</section>

{#if da && db}
  <section class="two">
    <Throughput series={digest(da).series} incidents={digest(da).incidents} since={da.since} until={da.until} />
    <Throughput series={digest(db).series} incidents={digest(db).incidents} since={db.since} until={db.until} />
  </section>
{/if}

<style>
  .head { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; }
  .title { display: flex; align-items: center; gap: 12px; }
  .back { font-size: 13px; }
  h2 { margin: 0; font-size: 17px; }
  .hd { display: flex; gap: 6px; align-items: center; margin-top: 6px; flex-wrap: wrap; text-transform: none; letter-spacing: 0; }
  .cmp th:not(:first-child), .cmp td:not(:first-child) { width: 32%; }
  .cmp td.num { font-size: 15px; font-weight: 600; }
  .win { color: var(--ok); }
  .lose { color: var(--bad); }
  .two { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 960px) { .two { grid-template-columns: 1fr; } }
</style>
