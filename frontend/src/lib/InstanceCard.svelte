<script>
  import LineChart from './LineChart.svelte';
  import HealthDot from './HealthDot.svelte';
  import { TAGS, TAG_BY_ID } from './tags.js';
  import { fmtAge, fmtNum, fmtDuration, shortName } from './format.js';

  let { instance, buckets = [], spells = [], bucketSec = 60, visible = {}, mode = 'smooth', onSelect } = $props();

  const counts = $derived(instance.counts || {});

  const longest = $derived(
    spells.length ? spells.reduce((a, b) => (b.duration_s > a.duration_s ? b : a)) : null
  );
  const totalIssueTime = $derived(
    spells.reduce((sum, s) => sum + (s.duration_s || 0), 0)
  );
  const ongoing = $derived(
    spells.some(s => (Date.now() / 1000 - s.end_ts) <= 10)
  );
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
      <span class="k">Issue time</span>
      <span class="v mono" class:bad={totalIssueTime > 0}>
        {totalIssueTime > 0 ? fmtDuration(totalIssueTime) : '—'}
      </span>
    </div>
  </div>

  {#if longest}
    {@const t = TAG_BY_ID[longest.tag] || { color: 'var(--fg-dim)', label: longest.tag }}
    <div class="spell" style:--c={t.color}>
      <span class="dot"></span>
      <span class="lbl">{t.label}</span>
      <span class="dur mono">{fmtDuration(longest.duration_s)}</span>
      {#if ongoing}<span class="now">ongoing</span>{:else}<span class="dim mono">×{longest.count}</span>{/if}
    </div>
  {/if}

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

  .spell {
    display: flex; align-items: center; gap: 8px;
    padding: 6px 10px;
    background: color-mix(in srgb, var(--c) 8%, var(--bg-3));
    border: 1px solid color-mix(in srgb, var(--c) 25%, var(--line));
    border-radius: 8px;
    font-size: 11.5px;
    margin-top: -2px;
  }
  .spell .dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--c);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--c) 22%, transparent);
  }
  .spell .lbl {
    color: var(--c);
    font-size: 10px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.06em;
  }
  .spell .dur {
    margin-left: auto;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    color: var(--fg);
  }
  .spell .now {
    font-size: 9.5px; font-weight: 700;
    color: #ffd28a;
    background: rgba(245,177,74,0.18);
    padding: 1px 7px; border-radius: 999px;
    text-transform: uppercase; letter-spacing: 0.08em;
    animation: pulse 1.6s ease-in-out infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50%      { opacity: 0.55; }
  }
</style>
