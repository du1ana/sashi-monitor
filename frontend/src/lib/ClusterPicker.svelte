<script>
  import { api } from './api.js';
  import { fmtTime } from './format.js';

  let { clusters = $bindable([]), activeId = $bindable(null), onChange } = $props();

  let busy = $state(false);
  let discoverErr = $state('');
  let lastDiscover = $state(null);

  function shortId(id) {
    if (!id) return '—';
    if (id === '_unknown') return '(no contract_id)';
    return id.slice(0, 8) + '…' + id.slice(-4);
  }
  function shortHash(name) {
    if (!name) return '—';
    return name.length > 14 ? name.slice(0, 6) + '…' + name.slice(-4) : name;
  }
  function shortImage(img) {
    if (!img) return '—';
    return img.replace(/^evernode(?:dev)?\//, '').replace(/^evernode\//, '');
  }

  async function toggle(cluster) {
    if (busy) return;
    busy = true;
    try {
      const next = !cluster.monitored;
      cluster.monitored = next;          // optimistic
      await api.setClusterMonitored(cluster.contract_id, next);
      onChange?.();
    } catch (e) {
      cluster.monitored = !cluster.monitored;
      discoverErr = e.message || String(e);
    } finally {
      busy = false;
    }
  }

  async function discover() {
    if (busy) return;
    busy = true; discoverErr = '';
    try {
      const r = await api.discoverNow();
      lastDiscover = r;
      onChange?.();
    } catch (e) {
      discoverErr = e.message || String(e);
    } finally {
      busy = false;
    }
  }

  let monitoredCount = $derived(clusters.filter(c => c.monitored).length);
  let totalNodes     = $derived(clusters.reduce((a, c) => a + (c.node_count || 0), 0));
  let monitoredNodes = $derived(
    clusters.filter(c => c.monitored).reduce((a, c) => a + (c.node_count || 0), 0)
  );

  // Sort: monitored first, then by node_count desc, then by last_seen desc.
  let sorted = $derived(
    [...clusters].sort((a, b) =>
      (Number(b.monitored) - Number(a.monitored))
      || (b.node_count - a.node_count)
      || ((b.last_seen || 0) - (a.last_seen || 0))
    )
  );
</script>

<section class="picker">
  <div class="picker-head">
    <div class="picker-title">
      <h2>Clusters</h2>
      <span class="picker-sub dim">
        {monitoredCount}/{clusters.length} monitored
        · {monitoredNodes}/{totalNodes} nodes tailed
      </span>
    </div>
    <div class="picker-tools">
      {#if discoverErr}<span class="err-pill" title={discoverErr}>error</span>{/if}
      {#if lastDiscover}
        <span class="dim hint">
          {lastDiscover.instances_seen ?? 0} nodes · {lastDiscover.clusters_seen ?? 0} clusters
          {#if lastDiscover.tails_started}· +{lastDiscover.tails_started} tailed{/if}
          {#if lastDiscover.tails_reaped}· -{lastDiscover.tails_reaped} dropped{/if}
          {#if lastDiscover.purged?.instances}· purged {lastDiscover.purged.instances} stale{/if}
          {#if lastDiscover.empty_clusters_dropped}· -{lastDiscover.empty_clusters_dropped} empty clusters{/if}
        </span>
      {/if}
      <button class="pri-btn" onclick={discover} disabled={busy} title="Run `sashi list` now">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 4v6h-6"/>
        </svg>
        <span>{busy ? 'Discovering…' : 'Discover'}</span>
      </button>
    </div>
  </div>

  {#if !clusters.length}
    <div class="empty">
      <p>No clusters discovered yet.</p>
      <p class="dim">Click <b>Discover</b> to run <code>sashi list</code>.</p>
    </div>
  {:else}
    <ul class="cluster-list">
      <li
        class="cluster all-row"
        class:active={activeId == null}
      >
        <button class="row-main" onclick={() => activeId = null}>
          <span class="dot all"></span>
          <div class="meta">
            <div class="line1"><b>All monitored</b></div>
            <div class="line2 dim">{monitoredCount} clusters · {monitoredNodes} nodes</div>
          </div>
          {#if activeId == null}<span class="chev">●</span>{/if}
        </button>
      </li>
      {#each sorted as c (c.contract_id)}
        <li
          class="cluster"
          class:monitored={c.monitored}
          class:active={activeId === c.contract_id}
        >
          <label class="toggle" title={c.monitored ? 'Stop monitoring' : 'Start monitoring'}>
            <input
              type="checkbox"
              checked={c.monitored}
              onchange={() => toggle(c)}
              disabled={busy}
            />
            <span class="sw"></span>
          </label>
          <button class="row-main" onclick={() => activeId = c.contract_id} disabled={!c.monitored}>
            <div class="meta">
              <div class="line1">
                <code class="cid">{shortId(c.contract_id)}</code>
                <span class="nodes-pill" class:hot={c.node_count > 0}>
                  {c.node_count} node{c.node_count === 1 ? '' : 's'}
                </span>
                {#if c.label}<span class="label">{c.label}</span>{/if}
              </div>
              <div class="line2 dim">
                {#if c.tenants?.length}
                  <span class="tenant"><b>tenant</b> {c.tenants.map(t => shortHash(t)).join(', ')}</span>
                {/if}
                {#if c.images?.length}
                  <span class="image"><b>image</b> {c.images.map(shortImage).join(', ')}</span>
                {/if}
                {#if c.last_seen}
                  <span><b>seen</b> {fmtTime(c.last_seen)}</span>
                {/if}
              </div>
            </div>
            {#if activeId === c.contract_id}<span class="chev">●</span>{/if}
          </button>
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .picker {
    background: linear-gradient(180deg, var(--bg-1), var(--bg-2));
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 16px 18px 14px;
    margin-bottom: 18px;
    box-shadow: var(--shadow-1);
  }
  .picker-head {
    display: flex; align-items: center; justify-content: space-between;
    gap: 12px; flex-wrap: wrap; margin-bottom: 10px;
  }
  .picker-title { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
  .picker-title h2 {
    margin: 0; font-size: 13px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.08em;
  }
  .picker-sub { font-size: 11px; font-variant-numeric: tabular-nums; }
  .picker-tools { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .hint { font-size: 10.5px; font-variant-numeric: tabular-nums; }
  .err-pill {
    background: rgba(255,93,108,0.18); color: #ff97a2;
    font-size: 10px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.08em;
    padding: 2px 8px; border-radius: 999px;
  }
  .pri-btn {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 5px 12px;
    background: color-mix(in srgb, var(--green) 12%, var(--bg-2));
    border: 1px solid color-mix(in srgb, var(--green) 35%, var(--line));
    border-radius: 9px;
    color: var(--fg);
    font-size: 11px; font-weight: 600;
    cursor: pointer;
    transition: background .15s, transform .12s, border-color .15s;
  }
  .pri-btn:hover:not(:disabled) {
    background: color-mix(in srgb, var(--green) 22%, var(--bg-2));
    border-color: var(--green);
  }
  .pri-btn:active:not(:disabled) { transform: scale(0.97); }
  .pri-btn:disabled { opacity: 0.6; cursor: progress; }
  .pri-btn svg { color: var(--green); }

  .empty { padding: 20px 0; font-size: 12px; }
  .empty p { margin: 4px 0; }
  .empty code {
    background: var(--bg-3); padding: 1px 6px; border-radius: 4px;
    font-family: var(--mono); font-size: 11px;
  }

  .cluster-list {
    list-style: none; padding: 0; margin: 0;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 8px;
  }
  .cluster {
    display: flex; align-items: stretch; gap: 0;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: var(--bg-2);
    overflow: hidden;
    transition: border-color .15s, background .15s;
  }
  .cluster.monitored { border-color: color-mix(in srgb, var(--green) 35%, var(--line)); }
  .cluster.active {
    border-color: var(--green);
    box-shadow: 0 0 0 1px var(--green) inset;
  }
  .cluster.all-row { grid-column: 1 / -1; }
  .toggle {
    flex: 0 0 auto;
    display: flex; align-items: center; padding: 0 12px;
    background: var(--bg-3);
    border-right: 1px solid var(--line);
    cursor: pointer;
  }
  .toggle input { display: none; }
  .toggle .sw {
    width: 30px; height: 16px; border-radius: 999px;
    background: var(--line);
    position: relative;
    transition: background .15s;
  }
  .toggle .sw::after {
    content: '';
    position: absolute; left: 2px; top: 2px;
    width: 12px; height: 12px; border-radius: 50%;
    background: var(--fg-dim);
    transition: transform .15s, background .15s;
  }
  .toggle input:checked + .sw { background: color-mix(in srgb, var(--green) 50%, var(--bg-3)); }
  .toggle input:checked + .sw::after {
    transform: translateX(14px);
    background: var(--green);
  }
  .row-main {
    flex: 1 1 auto;
    display: flex; align-items: center; gap: 10px;
    padding: 10px 12px;
    background: transparent;
    border: 0;
    color: inherit;
    text-align: left;
    cursor: pointer;
    transition: background .12s;
  }
  .row-main:hover:not(:disabled) { background: color-mix(in srgb, var(--green) 5%, transparent); }
  .row-main:disabled { cursor: default; opacity: 0.55; }
  .meta { flex: 1 1 auto; min-width: 0; }
  .line1 {
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
    font-size: 12px; font-weight: 600;
  }
  .line2 {
    margin-top: 3px;
    font-size: 10.5px;
    display: flex; flex-wrap: wrap; gap: 4px 12px;
  }
  .line2 b {
    font-weight: 700; color: var(--fg-dim);
    text-transform: uppercase; letter-spacing: 0.05em;
    font-size: 9.5px; margin-right: 3px;
  }
  .cid {
    font-family: var(--mono); font-size: 11px;
    color: var(--fg);
    background: var(--bg-3);
    padding: 1px 6px; border-radius: 4px;
  }
  .nodes-pill {
    font-size: 10px; font-weight: 700;
    padding: 1px 8px; border-radius: 999px;
    background: rgba(91,102,122,0.25); color: var(--fg-muted);
    font-variant-numeric: tabular-nums;
  }
  .nodes-pill.hot {
    background: color-mix(in srgb, var(--green) 18%, var(--bg-3));
    color: #69f0bd;
  }
  .label {
    font-size: 10.5px; font-weight: 600;
    color: var(--amber);
  }
  .dot {
    flex: 0 0 auto;
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--fg-dim);
  }
  .dot.all { background: var(--green); box-shadow: 0 0 6px color-mix(in srgb, var(--green) 60%, transparent); }
  .chev {
    flex: 0 0 auto;
    color: var(--green);
    font-size: 10px;
    text-shadow: 0 0 8px var(--green);
  }
</style>
