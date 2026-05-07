<script>
  import { TAG_BY_ID } from './tags.js';
  import { fmtDuration, fmtTime } from './format.js';

  let { spells = [], now = Date.now() / 1000, ongoingGap = 10 } = $props();

  // Largest duration sets bar scale.
  let maxDur = $derived(
    spells.reduce((m, s) => Math.max(m, s.duration_s || 0), 0) || 1,
  );

  function isOngoing(s) {
    return (now - s.end_ts) <= ongoingGap;
  }
</script>

{#if spells.length === 0}
  <div class="empty dim">No issue spells in this window.</div>
{:else}
  <div class="list">
    {#each spells as s, i}
      {@const tag = TAG_BY_ID[s.tag] || { color: 'var(--fg-dim)', label: s.tag }}
      {@const ongoing = isOngoing(s)}
      <div class="row" style:--c={tag.color}>
        <div class="head">
          <span class="tag">{tag.label}</span>
          {#if ongoing}<span class="ongoing">ongoing</span>{/if}
          <span class="dur mono">{fmtDuration(s.duration_s)}</span>
          <span class="count mono dim">{s.count} ev</span>
        </div>
        <div class="bar-wrap">
          <div class="bar" style:width={`${(s.duration_s / maxDur) * 100}%`}></div>
        </div>
        <div class="meta dim mono">
          {fmtTime(s.start_ts, true)} → {ongoing ? 'now' : fmtTime(s.end_ts, true)}
        </div>
      </div>
    {/each}
  </div>
{/if}

<style>
  .empty { padding: 18px; text-align: center; font-size: 12px; }
  .list { display: flex; flex-direction: column; gap: 6px; }
  .row {
    padding: 10px 12px;
    border-radius: 8px;
    background: color-mix(in srgb, var(--c) 6%, var(--bg-2));
    border: 1px solid color-mix(in srgb, var(--c) 25%, var(--line));
    display: flex; flex-direction: column; gap: 6px;
  }
  .head {
    display: flex; align-items: center; gap: 10px;
    flex-wrap: wrap;
  }
  .tag {
    color: var(--c);
    font-size: 10.5px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.06em;
  }
  .ongoing {
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
  .dur {
    font-size: 13px; font-weight: 700;
    color: var(--fg);
    margin-left: auto;
    font-variant-numeric: tabular-nums;
  }
  .count { font-size: 10.5px; }

  .bar-wrap {
    height: 4px;
    background: rgba(255,255,255,0.04);
    border-radius: 2px;
    overflow: hidden;
  }
  .bar {
    height: 100%;
    background: var(--c);
    border-radius: 2px;
    transition: width .25s ease;
  }
  .meta {
    font-size: 10px;
  }
</style>
