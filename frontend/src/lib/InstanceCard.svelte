<script>
  import LineChart from './LineChart.svelte';
  import HealthDot from './HealthDot.svelte';
  import { TAGS } from './tags.js';
  import { fmtAge, fmtNum, shortName } from './format.js';

  let { instance, buckets = [], bucketSec = 60, visible = {}, mode = 'smooth', onSelect } = $props();

  const counts = $derived(instance.counts || {});
</script>

<button class="card" onclick={() => onSelect?.(instance)} aria-label="Open {instance.name}">
  <div class="head">
    <div class="title">
      <span class="name mono">{shortName(instance.name, 16)}</span>
      <span class="ellipsis dim">…{instance.name.slice(-4)}</span>
    </div>
    <HealthDot health={instance.health} size="sm" />
  </div>

  <div class="meta">
    <div>
      <span class="k">Last ledger</span>
      <span class="v mono">{fmtAge(instance.last_ledger_age_s)}</span>
    </div>
    <div>
      <span class="k">Uptime</span>
      <span class="v mono">{instance.uptime_pct ?? 0}%</span>
    </div>
    <div>
      <span class="k">Errors</span>
      <span class="v mono" class:bad={(counts.error||0) + (counts.fork_warn||0) > 0}>
        {fmtNum((counts.error||0) + (counts.fork_warn||0) + (counts.consensus_lost||0))}
      </span>
    </div>
  </div>

  <div class="chart">
    <LineChart {buckets} {bucketSec} tags={TAGS} {visible} {mode} height={130} />
  </div>
</button>

<style>
  .card {
    display: flex; flex-direction: column; gap: 12px;
    background: linear-gradient(180deg, var(--bg-1), var(--bg-2));
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 16px;
    text-align: left; width: 100%;
    transition: transform .18s ease, border-color .18s, box-shadow .18s;
    box-shadow: var(--shadow-1);
  }
  .card:hover {
    border-color: var(--line-2);
    transform: translateY(-2px);
    box-shadow: var(--shadow-1), var(--shadow-2);
  }
  .head {
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
  }
  .title {
    display: flex; align-items: baseline; gap: 4px;
    min-width: 0;
  }
  .name {
    font-size: 13px; font-weight: 600;
    letter-spacing: 0.02em;
  }
  .ellipsis {
    font-family: var(--mono);
    font-size: 11px;
  }
  .meta {
    display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;
    font-size: 11px;
  }
  .meta div { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .k { color: var(--fg-dim); text-transform: uppercase; letter-spacing: 0.06em; font-size: 9.5px; font-weight: 600; }
  .v { color: var(--fg); font-size: 13px; font-variant-numeric: tabular-nums; }
  .v.bad { color: var(--red); }
  .chart { margin-top: 2px; }
</style>
