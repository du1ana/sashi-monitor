<script>
  import { HEALTH } from './tags.js';
  let { health = 'unknown', size = 'md' } = $props();
  let h = $derived(HEALTH[health] || HEALTH.unknown);
</script>

<span class="pill" class:sm={size === 'sm'} style:--bg={h.bg} style:--fg={h.fg} style:--dot={h.dot}>
  <span class="dot"></span>
  <span class="label">{h.label}</span>
</span>

<style>
  .pill {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 3px 10px 3px 8px;
    border-radius: 999px;
    background: var(--bg);
    color: var(--fg);
    font-size: 11px; font-weight: 600;
    letter-spacing: 0.02em;
    text-transform: lowercase;
    line-height: 1.3;
    white-space: nowrap;
  }
  .sm { padding: 2px 8px 2px 6px; font-size: 10px; }
  .dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--dot);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--dot) 25%, transparent);
  }
  .label { font-feature-settings: "ss01"; }
</style>
