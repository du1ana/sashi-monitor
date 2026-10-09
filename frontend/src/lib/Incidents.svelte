<script>
  import { incMeta, clock, dur } from './util.js';
  import IncidentDrawer from './IncidentDrawer.svelte';
  let { incidents, until } = $props();

  const KINDS = ['fork', 'stall', 'height_split', 'freeze', 'refused_close', 'fast_forward', 'config_patch',
    'patch_after_sync', 'invalid_cert', 'conflicting_cert', 'node_start', 'error'];
  let hidden = $state(new Set(['node_start']));
  let open = $state(null);

  const counts = $derived(Object.fromEntries(KINDS.map((k) => [k, incidents.filter((i) => i.kind === k).length])));
  const shown = $derived(incidents.filter((i) => !hidden.has(i.kind)).slice(0, 300));
  function toggle(k) { const s = new Set(hidden); s.has(k) ? s.delete(k) : s.add(k); hidden = s; }
</script>

<section class="panel">
  <div class="panel-head">
    <h3>Incidents & events</h3>
    <div class="filters">
      {#each KINDS.filter((k) => counts[k]) as k}
        {@const m = incMeta(k)}
        <button class="pill {hidden.has(k) ? 'neutral off' : m.cls}" onclick={() => toggle(k)}>{m.icon} {m.label} <b>{counts[k]}</b></button>
      {/each}
    </div>
  </div>
  {#if !shown.length}
    <div class="empty">Nothing recorded in this window. That's good.</div>
  {:else}
    <div class="scroll">
      <table class="t">
        <thead><tr><th>Start (UTC)</th><th>Kind</th><th>Where</th><th>Duration</th><th>Summary</th><th></th></tr></thead>
        <tbody>
          {#each shown as i (i.host + i.id)}
            {@const m = incMeta(i.kind)}
            <tr class="click" onclick={() => (open = i)}>
              <td class="mono num">{clock(i.start_ts)}</td>
              <td><span class="pill {m.cls}">{m.icon} {m.label}</span></td>
              <td class="mono">{(i.hosts || [i.host]).join(', ')}{i.node ? ' / ' + i.node : ''}</td>
              <td class="num">{i.end_ts == null ? 'open · ' + dur(i.start_ts, null, until) : i.end_ts - i.start_ts < 1 ? '—' : dur(i.start_ts, i.end_ts)}</td>
              <td class="sum">{i.summary}</td>
              <td class="faint">{(i.captures || []).length ? '📄' : ''}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</section>

{#if open}<IncidentDrawer incident={open} onclose={() => (open = null)} />{/if}

<style>
  .filters { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
  .filters button { border: 0; cursor: pointer; }
  .filters .off { opacity: .55; text-decoration: line-through; }
  .scroll { max-height: 460px; overflow: auto; }
  .sum { max-width: 560px; }
  td .pill { font-size: 11.5px; }
</style>
