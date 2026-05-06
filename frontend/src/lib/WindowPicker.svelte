<script>
  let { value = $bindable('3600') } = $props();
  const opts = [
    { v: '900',   label: '15m' },
    { v: '3600',  label: '1h'  },
    { v: '21600', label: '6h'  },
    { v: '86400', label: '24h' },
    { v: 'all',   label: 'All' },
  ];
</script>

<div class="seg" role="tablist" aria-label="Time window">
  {#each opts as o}
    <button
      role="tab"
      class:active={value === o.v}
      aria-selected={value === o.v}
      onclick={() => (value = o.v)}
    >{o.label}</button>
  {/each}
</div>

<style>
  .seg {
    display: inline-flex;
    padding: 3px;
    background: var(--bg-2);
    border: 1px solid var(--line);
    border-radius: 12px;
    gap: 2px;
  }
  .seg button {
    padding: 6px 12px;
    border-radius: 9px;
    font-size: 12px; font-weight: 600;
    color: var(--fg-muted);
    transition: color .15s, background .15s, transform .12s;
    font-variant-numeric: tabular-nums;
  }
  .seg button:hover { color: var(--fg); }
  .seg button.active {
    background: var(--bg-3);
    color: var(--fg);
    box-shadow: var(--shadow-1);
  }
</style>
