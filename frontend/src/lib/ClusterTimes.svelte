<script>
  // Start, running time, expiry and missing nodes of one cluster (overview record).
  import { ago, clock, until, expiryClass } from './util.js';
  let { c, now } = $props();

  const exp = $derived(until(c.expiry_ts, now));
  const gone = $derived(c.peak_nodes != null && c.nodes != null ? Math.max(0, c.peak_nodes - c.nodes) : 0);
</script>

<div class="times">
  {#if c.start_ts}
    <span title="When the real contract replaced the bootstrap contract">started {clock(c.start_ts)} · running {ago(now - c.start_ts)}</span>
  {:else}
    <span class="faint" title="The contract upload happened before the monitor saw this cluster">start: before monitoring</span>
  {/if}
  {#if !c.offline}
    {#if exp != null}
      <span class="exp {expiryClass(exp)}" title="Earliest lease expiry among this cluster's nodes">
        {exp > 0 ? `expires in ${ago(exp)}` : 'expiring now'}</span>
    {/if}
    {#if gone}
      <span class="pill bad" title="Nodes seen earlier that are no longer running (lease expired or instance deleted)">
        {gone} of {c.peak_nodes} nodes gone</span>
    {/if}
  {/if}
</div>

<style>
  .times { display: flex; flex-wrap: wrap; gap: 4px 12px; align-items: center; font-size: 12.5px; color: var(--muted); }
  .exp.warn { color: var(--warn); font-weight: 650; }
  .exp.bad { color: var(--bad); font-weight: 700; }
</style>
