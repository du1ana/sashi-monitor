<script>
  import LineChart from './LineChart.svelte';
  import HealthDot from './HealthDot.svelte';
  import { TAGS } from './tags.js';
  import { fmtAge, fmtDuration, shortName } from './format.js';

  let {
    instance,
    buckets = [],
    spells = [],
    bucketSec = 60,
    visible = {},
    mode = 'smooth',
    onSelect,
    canDelete = false,
    contractId = null,
    onDelete,
  } = $props();

  const totalIssueTime = $derived(
    spells.reduce((sum, s) => sum + (s.duration_s || 0), 0)
  );

  function clickDelete(e) {
    e.stopPropagation();
    onDelete?.(instance, contractId);
  }
</script>

<div class="card-wrap">
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
        <span class="k">Total error</span>
        <span class="v mono" class:bad={totalIssueTime > 0}>
          {totalIssueTime > 0 ? fmtDuration(totalIssueTime) : '—'}
        </span>
      </div>
    </div>

    <div class="chart">
      <LineChart {buckets} {bucketSec} tags={TAGS} {visible} {mode} height={130} />
    </div>
  </button>

  {#if canDelete}
    <div class="actions">
      <button class="del-btn" onclick={clickDelete} title="Cluster is hard-forked. Run `evernode delete` on this node and verify via `sashi list`.">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M3 6h18"/>
          <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
          <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
        </svg>
        <span>Delete instance</span>
      </button>
    </div>
  {/if}
</div>

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

  .card-wrap { display: flex; flex-direction: column; gap: 6px; }
  .actions { display: flex; justify-content: flex-end; padding: 0 4px; }
  .del-btn {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 5px 10px;
    background: color-mix(in srgb, var(--red) 8%, var(--bg-1));
    border: 1px solid color-mix(in srgb, var(--red) 35%, var(--line));
    border-radius: 9px;
    color: #ff97a2;
    font-size: 11px; font-weight: 600;
    cursor: pointer;
    transition: background .15s, border-color .15s, transform .12s;
  }
  .del-btn:hover:not(:disabled) {
    background: color-mix(in srgb, var(--red) 18%, var(--bg-1));
    border-color: var(--red);
    color: #fff;
  }
  .del-btn:active:not(:disabled) { transform: scale(0.97); }
  .del-btn:disabled { opacity: 0.55; cursor: progress; }
  .del-btn svg { color: var(--red); }
</style>
