<script>
  import { onMount } from 'svelte';
  import { getJSON } from './lib/util.js';
  import HostStrip from './lib/HostStrip.svelte';
  import Overview from './lib/Overview.svelte';
  import ClusterView from './lib/ClusterView.svelte';
  import Compare from './lib/Compare.svelte';

  let overview = $state(null);
  let error = $state(null);
  let route = $state(parse(location.hash));
  let theme = $state(localStorageGet('sashimon-theme') || 'auto');

  function localStorageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
  function parse(hash) {
    const p = hash.replace(/^#\/?/, '').split('/').filter(Boolean);
    if (p[0] === 'c' && p[1]) return { view: 'cluster', id: decodeURIComponent(p[1]) };
    if (p[0] === 'compare' && p[1] && p[2]) return { view: 'compare', a: decodeURIComponent(p[1]), b: decodeURIComponent(p[2]) };
    return { view: 'overview' };
  }

  async function load() {
    try {
      overview = await getJSON('api/overview');
      error = null;
    } catch (e) {
      error = String(e.message || e);
    }
  }

  $effect(() => {
    const el = document.documentElement;
    if (theme === 'auto') el.removeAttribute('data-theme'); else el.setAttribute('data-theme', theme);
    try { localStorage.setItem('sashimon-theme', theme); } catch {}
  });

  onMount(() => {
    load();
    const t = setInterval(load, 5000);
    const onHash = () => (route = parse(location.hash));
    window.addEventListener('hashchange', onHash);
    return () => { clearInterval(t); window.removeEventListener('hashchange', onHash); };
  });
</script>

<header>
  <a class="brand" href="#/">
    <svg viewBox="0 0 32 32" width="22" height="22" aria-hidden="true"><circle cx="16" cy="16" r="12" fill="none" stroke="var(--ok)" stroke-width="4"/><circle cx="16" cy="16" r="4" fill="var(--ok)"/></svg>
    <span>sashimon</span>
    <span class="sub">consensus observer</span>
  </a>
  <div class="right">
    {#if overview}<span class="faint mono">v{overview.version} · {overview.host}</span>{/if}
    <div class="seg" title="Theme">
      {#each ['auto', 'light', 'dark'] as t}
        <button class:on={theme === t} onclick={() => (theme = t)}>{t}</button>
      {/each}
    </div>
  </div>
</header>

<main>
  {#if error && !overview}
    <div class="panel empty">Cannot reach sashimon API: {error}</div>
  {:else if !overview}
    <div class="panel empty">Loading…</div>
  {:else}
    <HostStrip hosts={overview.hosts} {error} />
    {#if route.view === 'cluster'}
      <ClusterView id={route.id} {overview} />
    {:else if route.view === 'compare'}
      <Compare a={route.a} b={route.b} {overview} />
    {:else}
      <Overview {overview} />
    {/if}
  {/if}
</main>

<style>
  header {
    display: flex; align-items: center; justify-content: space-between; gap: 16px;
    padding: 12px 24px; border-bottom: 1px solid var(--border); background: var(--panel);
    position: sticky; top: 0; z-index: 10;
  }
  .brand { display: flex; align-items: center; gap: 10px; color: var(--text); font-weight: 700; font-size: 16px; }
  .brand .sub { color: var(--muted); font-weight: 500; font-size: 13px; }
  .right { display: flex; align-items: center; gap: 14px; }
  main { max-width: 1440px; margin: 0 auto; padding: 20px 24px 60px; display: grid; gap: 18px; }
  @media (max-width: 640px) {
    header { padding: 10px 16px; }
    .brand .sub { display: none; }
    main { padding: 14px 16px 40px; }
  }
</style>
