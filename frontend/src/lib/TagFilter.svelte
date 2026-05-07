<script>
  import { TAGS } from './tags.js';
  let {
    visible = $bindable({}),
    counts = {},
    disabledIds = new Set(),
    disabledHint = '',
  } = $props();

  function toggle(id) {
    if (disabledIds.has(id)) return;
    visible = { ...visible, [id]: visible[id] === false ? true : false };
  }
  function only(id) {
    if (disabledIds.has(id)) return;
    const next = {};
    for (const t of TAGS) next[t.id] = (t.id === id) && !disabledIds.has(t.id);
    visible = next;
  }
  function all() {
    const next = {};
    for (const t of TAGS) next[t.id] = !disabledIds.has(t.id);
    visible = next;
  }
</script>

<div class="bar">
  <button class="meta" onclick={all} title="Show all">All</button>
  <span class="sep"></span>
  {#each TAGS as t}
    {@const on = visible[t.id] !== false}
    {@const c = counts[t.id] || 0}
    {@const dis = disabledIds.has(t.id)}
    <button
      class="chip"
      class:off={!on}
      class:disabled={dis}
      style:--c={t.color}
      onclick={() => toggle(t.id)}
      ondblclick={() => only(t.id)}
      disabled={dis}
      title={dis ? (disabledHint || 'Disabled') : `${t.label} — click to toggle, double-click to isolate`}
    >
      <span class="swatch"></span>
      <span class="label">{t.label}</span>
      {#if c > 0}<span class="count">{c}</span>{/if}
    </button>
  {/each}
</div>

<style>
  .bar {
    display: flex; flex-wrap: wrap; gap: 6px; align-items: center;
  }
  .sep {
    width: 1px; height: 18px; background: var(--line); margin: 0 4px;
  }
  .meta {
    padding: 5px 10px; font-size: 11px; font-weight: 600;
    color: var(--fg-muted);
    background: var(--bg-2); border: 1px solid var(--line);
    border-radius: 999px;
    transition: color .15s, border-color .15s;
  }
  .meta:hover { color: var(--fg); border-color: var(--line-2); }

  .chip {
    display: inline-flex; align-items: center; gap: 7px;
    padding: 5px 10px 5px 8px;
    background: var(--bg-2);
    border: 1px solid var(--line);
    border-radius: 999px;
    font-size: 11.5px;
    color: var(--fg);
    transition: transform .12s, border-color .15s, background .15s, opacity .15s;
  }
  .chip:hover { border-color: var(--c); transform: translateY(-1px); }
  .chip.off { opacity: .42; }
  .chip.off .swatch { opacity: .3; }
  .chip.disabled {
    opacity: .25;
    cursor: not-allowed;
    pointer-events: auto;
  }
  .chip.disabled:hover { transform: none; border-color: var(--line); }

  .swatch {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--c);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--c) 22%, transparent);
  }
  .label { font-weight: 500; }
  .count {
    font-family: var(--mono);
    font-size: 10px;
    padding: 1px 6px;
    background: color-mix(in srgb, var(--c) 16%, transparent);
    color: var(--c);
    border-radius: 999px;
    font-variant-numeric: tabular-nums;
    margin-left: 2px;
  }
</style>
