<script>
  import { onMount, onDestroy } from 'svelte';
  import { api } from './api.js';
  import { TAG_BY_ID } from './tags.js';
  import { fmtTime, shortName } from './format.js';
  import HealthDot from './HealthDot.svelte';

  import Spells from './Spells.svelte';

  let { instance, window: windowVal = '3600', onClose } = $props();

  let events = $state([]);
  let spells = $state([]);
  let loading = $state(true);
  let err = $state('');
  let timer;

  async function load() {
    try {
      loading = true;
      const [ev, sp] = await Promise.all([
        api.events({ instance: instance.name, limit: 300 }),
        api.spells({ instance: instance.name, window: windowVal }),
      ]);
      events = ev;
      spells = sp;
      err = '';
    } catch (e) {
      err = String(e.message || e);
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    load();
    timer = setInterval(load, 5000);
    document.body.style.overflow = 'hidden';
    document.addEventListener('keydown', onKey);
  });

  onDestroy(() => {
    clearInterval(timer);
    document.body.style.overflow = '';
    document.removeEventListener('keydown', onKey);
  });

  function onKey(e) {
    if (e.key === 'Escape') onClose?.();
  }
</script>

<div class="scrim" onclick={() => onClose?.()} role="presentation"></div>
<div class="drawer" role="dialog" aria-modal="true" aria-label="Instance details">
  <header>
    <div class="left">
      <div class="title">
        <span class="mono name">{shortName(instance.name, 24)}</span>
        <HealthDot health={instance.health} size="sm" />
      </div>
      <div class="sub mono dim">{instance.name}</div>
    </div>
    <button class="close" onclick={() => onClose?.()} aria-label="Close">✕</button>
  </header>

  <div class="grid">
    <div><span class="k">Sashi status</span><span class="v">{instance.sashi_status || '—'}</span></div>
    <div><span class="k">Uptime</span><span class="v mono">{instance.uptime_pct ?? 0}%</span></div>
    <div><span class="k">Last ledger</span><span class="v mono">{instance.last_ledger_age_s != null ? instance.last_ledger_age_s.toFixed(1) + 's' : '—'} ago</span></div>
    <div><span class="k">Last event</span><span class="v mono">{instance.last_event_age_s != null ? instance.last_event_age_s.toFixed(1) + 's' : '—'} ago</span></div>
  </div>

  <h3>Issue spells</h3>
  <div class="spells-wrap">
    {#if loading && spells.length === 0}
      {#each Array(3) as _}<div class="row skel" style="height:46px"></div>{/each}
    {:else}
      <Spells {spells} />
    {/if}
  </div>

  <h3>Recent events</h3>
  <div class="log scroll">
    {#if loading && events.length === 0}
      {#each Array(10) as _}<div class="row skel" style="height:18px"></div>{/each}
    {:else if err}
      <div class="err">{err}</div>
    {:else if events.length === 0}
      <div class="empty dim">No events yet.</div>
    {:else}
      {#each events as e}
        {@const tag = TAG_BY_ID[e.tag] || { color: 'var(--fg-dim)', label: e.tag }}
        <div class="row" style:--c={tag.color}>
          <span class="ts mono">{fmtTime(e.ts, true)}</span>
          <span class="tag" style:--c={tag.color}>{tag.label}</span>
          <span class="msg mono">{e.msg}</span>
        </div>
      {/each}
    {/if}
  </div>
</div>

<style>
  .scrim {
    position: fixed; inset: 0; background: rgba(0,0,0,0.55);
    backdrop-filter: blur(2px);
    z-index: 40;
    animation: fadeIn .18s ease;
  }
  .drawer {
    position: fixed; right: 0; top: 0; bottom: 0;
    width: min(720px, 100%);
    background: var(--bg-1);
    border-left: 1px solid var(--line);
    box-shadow: -20px 0 40px rgba(0,0,0,0.5);
    display: flex; flex-direction: column;
    z-index: 50;
    animation: slideIn .22s cubic-bezier(.2,.8,.2,1);
  }
  .drawer:focus { outline: none; }
  @keyframes fadeIn { from { opacity: 0; } }
  @keyframes slideIn { from { transform: translateX(20px); opacity: 0; } }

  header {
    display: flex; align-items: flex-start; justify-content: space-between;
    padding: 18px 20px;
    border-bottom: 1px solid var(--line);
  }
  .title { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .name  { font-size: 13px; font-weight: 600; }
  .sub   { font-size: 10.5px; word-break: break-all; margin-top: 6px; }

  .close {
    width: 32px; height: 32px; border-radius: 8px;
    color: var(--fg-muted);
    transition: background .15s, color .15s;
  }
  .close:hover { background: var(--bg-3); color: var(--fg); }

  .grid {
    display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px;
    padding: 16px 20px;
    border-bottom: 1px solid var(--line);
  }
  .grid > div { display: flex; flex-direction: column; gap: 4px; }
  .k { font-size: 9.5px; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; color: var(--fg-dim); }
  .v { font-size: 14px; }

  h3 { margin: 16px 20px 8px; font-size: 11px; font-weight: 600; color: var(--fg-dim); text-transform: uppercase; letter-spacing: 0.08em; }
  .spells-wrap { padding: 0 20px 4px; }

  .log {
    flex: 1; overflow-y: auto;
    padding: 0 20px 20px;
    display: flex; flex-direction: column; gap: 1px;
  }
  .row {
    display: grid;
    grid-template-columns: 130px 130px 1fr;
    gap: 10px; align-items: baseline;
    padding: 7px 8px;
    border-radius: 6px;
    font-size: 11.5px;
    border-left: 2px solid color-mix(in srgb, var(--c) 40%, transparent);
    background: color-mix(in srgb, var(--c) 4%, transparent);
  }
  .row:hover { background: color-mix(in srgb, var(--c) 9%, transparent); }
  .ts { color: var(--fg-dim); font-size: 10.5px; white-space: nowrap; }
  .tag {
    color: var(--c);
    font-size: 10px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.06em;
    white-space: nowrap;
  }
  .msg {
    color: var(--fg);
    font-size: 11.5px;
    word-break: break-word;
    white-space: pre-wrap;
  }
  .empty { padding: 30px; text-align: center; }
  .err { padding: 16px; color: var(--red); font-size: 12px; }
  .row.skel { display: block; margin: 4px 0; }

  @media (max-width: 720px) {
    .row { grid-template-columns: 1fr; gap: 4px; }
    .grid { grid-template-columns: 1fr; }
  }
</style>
