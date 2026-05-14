<script>
  import { onMount, onDestroy } from 'svelte';
  import { api } from './lib/api.js';
  import { TAGS, ERROR_TAG_IDS, ERROR_TAG_SET } from './lib/tags.js';
  import LineChart from './lib/LineChart.svelte';
  import InstanceCard from './lib/InstanceCard.svelte';
  import TagFilter from './lib/TagFilter.svelte';
  import WindowPicker from './lib/WindowPicker.svelte';
  import EventDrawer from './lib/EventDrawer.svelte';
  import ClusterPicker from './lib/ClusterPicker.svelte';
  import { fmtNum, fmtTime } from './lib/format.js';

  let windowVal = $state(
    (typeof localStorage !== 'undefined' && localStorage.getItem('sashimon.window')) || 'all'
  );
  let refreshMs = $state(5000);
  let chartMode = $state(
    (typeof localStorage !== 'undefined' && localStorage.getItem('sashimon.chartMode')) || 'smooth'
  );
  let granularity = $state(
    (typeof localStorage !== 'undefined' && localStorage.getItem('sashimon.granularity')) || 'minute'
  ); // 'minute' | 'hour'

  function toggleChartMode() {
    chartMode = chartMode === 'smooth' ? 'step' : 'smooth';
    try { localStorage.setItem('sashimon.chartMode', chartMode); } catch {}
  }

  function toggleGranularity() {
    granularity = granularity === 'minute' ? 'hour' : 'minute';
    try { localStorage.setItem('sashimon.granularity', granularity); } catch {}
    // In minute mode, force-hide non-error tags (perf safeguard).
    if (granularity === 'minute') {
      const next = {};
      for (const t of TAGS) next[t.id] = ERROR_TAG_SET.has(t.id) ? (visible[t.id] !== false) : false;
      visible = next;
    } else {
      // hour mode: re-enable all
      const next = {};
      for (const t of TAGS) next[t.id] = true;
      visible = next;
    }
  }
  let summary = $state([]);
  let globalBuckets = $state([]);
  let perInstanceBuckets = $state({});  // name -> [bucket]
  let perInstanceSpells  = $state({});  // name -> [spell]
  // Default: only error states visible (matches default minute granularity).
  let visible = $state(Object.fromEntries(TAGS.map(t => [t.id, ERROR_TAG_SET.has(t.id)])));
  let lastUpdate = $state(null);
  let loading = $state(true);
  let err = $state('');
  let selected = $state(null);
  let clearing = $state(false);
  let timer;

  // Tracking policy + DB size.
  // `full`: keep every event during every spell. `balanced` (default): drop
  // low-severity event floods during a spell; only fork-class spells trigger
  // metric boost + diagnostic snapshots. `minimal`: only store fork-class
  // events; never boost or snapshot.
  let policyMode    = $state('balanced');
  let policyModes   = $state(['balanced', 'full', 'minimal']);
  let dbSizeHuman   = $state('—');
  let dbSizeBytes   = $state(0);
  let policySaving  = $state(false);

  async function loadPolicy() {
    try {
      const p = await api.policy();
      policyMode  = p.mode || 'balanced';
      policyModes = Array.isArray(p.modes) && p.modes.length ? p.modes : ['balanced', 'full', 'minimal'];
    } catch {}
  }
  async function loadDbSize() {
    try {
      const s = await api.dbSize();
      dbSizeHuman = s.human || '—';
      dbSizeBytes = s.bytes || 0;
    } catch {}
  }
  async function changePolicy(next) {
    if (next === policyMode || policySaving) return;
    const prev = policyMode;
    policyMode = next;
    policySaving = true;
    try {
      await api.setPolicy(next);
    } catch (e) {
      policyMode = prev;
      err = 'policy: ' + (e.message || e);
    } finally {
      policySaving = false;
    }
  }

  // Cluster discovery / monitoring.
  function _initialCluster() {
    if (typeof localStorage === 'undefined') return null;
    const v = localStorage.getItem('sashimon.cluster');
    return v && v !== '__all__' ? v : null;
  }
  let clusters = $state([]);
  let activeCluster = $state(_initialCluster());
  let clustersLoaded = $state(false);

  async function loadClusters() {
    try {
      clusters = await api.clusters();
      // If the previously-selected cluster is gone (or now unmonitored), fall
      // back to "all monitored".
      if (activeCluster) {
        const c = clusters.find(x => x.contract_id === activeCluster);
        if (!c || !c.monitored) activeCluster = null;
      }
    } catch (e) {
      err = 'clusters: ' + (e.message || e);
    } finally {
      clustersLoaded = true;
    }
  }

  function onClusterChange() {
    // Toggle/select happened in ClusterPicker — refresh data + persist filter.
    try { localStorage.setItem('sashimon.cluster', activeCluster || ''); } catch {}
    loadClusters();
    refresh();
  }

  let monitoredClusters = $derived(clusters.filter(c => c.monitored));
  let activeClusterMeta = $derived(
    activeCluster ? clusters.find(c => c.contract_id === activeCluster) : null
  );
  // instance.name -> contract_id, and hard-forked contract_id set.
  let instanceCidMap = $derived.by(() => {
    const m = new Map();
    for (const c of clusters) for (const i of (c.instances || [])) m.set(i.name, c.contract_id);
    return m;
  });
  let hardForkCids = $derived(new Set(clusters.filter(c => c.hard_forked).map(c => c.contract_id)));

  // ---- Delete-instance modal state -----------------------------------
  let delTarget   = $state(null);       // {name, contract_id}
  let delBusy     = $state(false);
  let delResult   = $state(null);

  function askDelete(instance, cid) {
    delTarget = { name: instance.name, contract_id: cid || instanceCidMap.get(instance.name) };
    delResult = null;
  }
  function closeDelete() {
    if (delBusy) return;
    delTarget = null;
    delResult = null;
  }
  async function confirmDelete() {
    if (!delTarget || delBusy) return;
    delBusy = true;
    delResult = { status: 'running', text: 'running evernode delete (this can take a minute)…' };
    try {
      const r = await api.deleteInstance(delTarget.name);
      delResult = {
        status:  r.ok ? 'ok' : 'bad',
        text:    r.ok
          ? `Deleted. exit ${r.exit_code}; sashi list confirms instance gone.`
          : `Failed: ${r.error || ('still present, exit ' + r.exit_code)}`,
        transcript: r.transcript || '',
      };
      if (r.ok) {
        await loadClusters();
        await refresh();
      }
    } catch (e) {
      delResult = { status: 'bad', text: 'Request failed: ' + (e.message || e) };
    } finally {
      delBusy = false;
    }
  }

  // ---- Self-update modal state ---------------------------------------
  let updOpen   = $state(false);
  let updBusy   = $state(false);
  let updStatus = $state('');

  function askUpdate() { updOpen = true; updStatus = ''; }
  function closeUpdate() { if (!updBusy) { updOpen = false; updStatus = ''; } }
  async function confirmUpdate() {
    if (updBusy) return;
    updBusy = true;
    updStatus = 'spawning installer…';
    try {
      await api.selfUpdate();
      updStatus = 'installer running. waiting for service to come back…';
      let downSeen = false;
      for (let i = 0; i < 90; i++) {
        await new Promise(r => setTimeout(r, 2000));
        const ok = await api.healthz();
        if (ok && downSeen) {
          updStatus = 'service is back. reloading…';
          setTimeout(() => location.reload(), 1500);
          return;
        }
        if (!ok) downSeen = true;
        updStatus = `installer running… (~${(i + 1) * 2}s)`;
      }
      updStatus = 'service did not come back within ~180s. check /tmp/sashimon-update.log on the host.';
    } catch (e) {
      updStatus = 'update failed: ' + (e.message || e);
    } finally {
      updBusy = false;
    }
  }

  const POLICY_LABELS = {
    full:     'Full — record everything',
    balanced: 'Balanced — slim low-impact spells',
    minimal:  'Minimal — fork-class only',
  };
  const POLICY_HINTS = {
    full:     'Every event stored; every spell boosts metrics + captures diagnostic snapshots.',
    balanced: 'Default. Low-impact spells (consensus_lost, out_of_sync, warning) drop their event flood and skip metric-boost + snapshots. Forks get the full treatment.',
    minimal:  'Only ledger + fork-class events are stored. Never boosts metrics, never captures snapshots.',
  };

  async function clearDb() {
    if (clearing) return;
    const ok = typeof confirm === 'function' && confirm(
      `Clear all monitoring data (${dbSizeHuman})?\n\n` +
      'This wipes every event and instance row from the database. ' +
      'Tail processes keep running, so live instances reappear within ~30s.\n\n' +
      'This cannot be undone.'
    );
    if (!ok) return;
    clearing = true;
    try {
      const result = await api.clearAll();
      if (result && result.db_size_human) dbSizeHuman = result.db_size_human;
      // Reset client state so cards/charts vanish immediately.
      summary = [];
      globalBuckets = [];
      perInstanceBuckets = {};
      perInstanceSpells = {};
      err = '';
      await Promise.all([refresh(), loadDbSize()]);
    } catch (e) {
      err = String(e.message || e);
    } finally {
      clearing = false;
    }
  }

  let bucketSec = $derived(granularity === 'minute' ? 60 : 3600);

  // Disabled tag set — non-error tags can't be selected in minute mode (perf).
  let disabledIds = $derived(
    granularity === 'minute'
      ? new Set(TAGS.filter(t => !ERROR_TAG_SET.has(t.id)).map(t => t.id))
      : new Set()
  );

  async function refresh() {
    try {
      const w = windowVal;
      const cid = activeCluster || undefined;
      const [s, gb] = await Promise.all([
        api.summary(w, cid),
        api.histogram(w, bucketSec, undefined, cid),
      ]);
      // Scope to monitored clusters only — when no cluster filter is set,
      // the backend's /api/summary returns *every* instance (including ones
      // whose cluster is unmonitored but still in the `instances` table).
      // Filter those out client-side so the "All monitored" view is honest.
      let s2 = s;
      if (!cid) {
        const monitoredIds = new Set(clusters.filter(c => c.monitored).map(c => c.contract_id));
        // If we don't know cluster→instance mapping (no clusters loaded yet), keep all.
        if (monitoredIds.size) {
          const nameToCid = new Map();
          for (const c of clusters) {
            for (const i of (c.instances || [])) nameToCid.set(i.name, c.contract_id);
          }
          s2 = s.filter(inst => {
            const cidOf = nameToCid.get(inst.name);
            return cidOf == null || monitoredIds.has(cidOf);
          });
        }
      }
      summary = s2;
      globalBuckets = gb;

      // Per-instance histograms + spells (parallel per-instance, parallel per-call).
      const nextB = {};
      const nextS = {};
      await Promise.all(s2.map(async (inst) => {
        const [b, sp] = await Promise.all([
          api.histogram(w, bucketSec, inst.name),
          api.spells({ instance: inst.name, window: w }),
        ]);
        nextB[inst.name] = b;
        nextS[inst.name] = sp;
      }));
      perInstanceBuckets = nextB;
      perInstanceSpells  = nextS;

      lastUpdate = Date.now();
      err = '';
    } catch (e) {
      err = String(e.message || e);
    } finally {
      loading = false;
    }
  }

  function reschedule() {
    clearInterval(timer);
    if (refreshMs > 0) timer = setInterval(refresh, refreshMs);
  }

  // Aggregate counts across all instances (for TagFilter chips).
  let aggCounts = $derived.by(() => {
    const out = {};
    for (const inst of summary) {
      for (const [k, v] of Object.entries(inst.counts || {})) {
        out[k] = (out[k] || 0) + v;
      }
    }
    return out;
  });

  // Aggregate health counts.
  let healthCounts = $derived.by(() => {
    const out = { healthy: 0, consensus_loss: 0, forked: 0, stalled: 0, unknown: 0 };
    for (const inst of summary) out[inst.health] = (out[inst.health] || 0) + 1;
    return out;
  });

  let totalLedgers = $derived(aggCounts.ledger_created || 0);
  let totalErrors  = $derived((aggCounts.error || 0) + (aggCounts.fork_warn || 0) + (aggCounts.consensus_lost || 0));

  onMount(async () => {
    await loadClusters();
    refresh();
    reschedule();
    loadPolicy();
    loadDbSize();
  });
  onDestroy(() => clearInterval(timer));

  // Refresh DB size on the same cadence as the main refresh (cheap stat call).
  $effect(() => {
    void lastUpdate;
    loadDbSize();
  });

  // Re-run when window or granularity changes.
  $effect(() => {
    void windowVal;
    try { localStorage.setItem('sashimon.window', windowVal); } catch {}
    refresh();
  });
  $effect(() => { void granularity; refresh(); });
  $effect(() => { void refreshMs; reschedule(); });
  $effect(() => {
    void activeCluster;
    try { localStorage.setItem('sashimon.cluster', activeCluster || ''); } catch {}
    refresh();
  });
</script>

<div class="app">
  <header class="hdr">
    <div class="brand">
      <svg width="22" height="22" viewBox="0 0 64 64" aria-hidden="true">
        <rect width="64" height="64" rx="14" fill="var(--green)"/>
        <path d="M14 38l8-12 8 8 12-18 8 18" stroke="#06120e" stroke-width="5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <span class="name">sashimon</span>
      <span class="tag dim">Sashimono HotPocket monitor</span>
    </div>
    <div class="hdr-meta dim">
      {#if lastUpdate}updated {fmtTime(lastUpdate / 1000)}{:else}—{/if}
      {#if err}<span class="err-pill" title={err}>error</span>{/if}
      <label class="policy" title={POLICY_HINTS[policyMode] || ''}>
        <span class="policy-k">Tracking</span>
        <select
          class="policy-sel"
          value={policyMode}
          onchange={(e) => changePolicy(e.currentTarget.value)}
          disabled={policySaving}
          aria-label="DB tracking policy"
        >
          {#each policyModes as m}
            <option value={m}>{POLICY_LABELS[m] || m}</option>
          {/each}
        </select>
      </label>
      <button
        class="upd-btn"
        onclick={askUpdate}
        title="Re-run the install one-liner on this host. systemd restarts the service when done; the dashboard will reload itself."
        aria-label="Update sashi.mon"
      >
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 4v6h-6"/>
        </svg>
        <span>Update</span>
      </button>
      <button
        class="danger-btn"
        onclick={clearDb}
        disabled={clearing}
        title="Wipe all events and instance rows from the database. Live instances re-discover within ~30s."
        aria-label="Clear database"
      >
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M3 6h18"/>
          <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
          <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
          <path d="M10 11v6"/><path d="M14 11v6"/>
        </svg>
        <span>{clearing ? 'Clearing…' : 'Clear DB'}</span>
        <span class="db-size" aria-label="current db size">({dbSizeHuman})</span>
      </button>
    </div>
  </header>

  <ClusterPicker bind:clusters bind:activeId={activeCluster} onChange={onClusterChange} />

  {#if activeClusterMeta}
    <div class="cluster-banner">
      <span class="cb-label">Viewing cluster</span>
      <code class="cb-id">{activeClusterMeta.contract_id}</code>
      <span class="cb-meta dim">
        {activeClusterMeta.node_count} node{activeClusterMeta.node_count === 1 ? '' : 's'}
        {#if activeClusterMeta.images?.length} · {activeClusterMeta.images[0]}{/if}
      </span>
      <button class="cb-clear" onclick={() => { activeCluster = null; }}>view all monitored ×</button>
    </div>
  {/if}

  <section class="kpis">
    <div class="kpi">
      <span class="k">Instances</span>
      <span class="v">{summary.length}</span>
      <div class="health-row">
        {#each Object.entries(healthCounts) as [k, c]}
          {#if c > 0}
            <span class="hbit hbit-{k}">{c}</span>
          {/if}
        {/each}
      </div>
    </div>
    <div class="kpi">
      <span class="k">Ledgers <span class="muted">({windowVal === 'all' ? 'all-time' : 'window'})</span></span>
      <span class="v">{fmtNum(totalLedgers)}</span>
    </div>
    <div class="kpi">
      <span class="k">Issues <span class="muted">(errors + forks + cons-loss)</span></span>
      <span class="v" class:bad={totalErrors > 0}>{fmtNum(totalErrors)}</span>
    </div>
    <div class="kpi window">
      <span class="k">Window</span>
      <WindowPicker bind:value={windowVal} />
    </div>
    <div class="kpi window">
      <span class="k">Refresh</span>
      <select bind:value={refreshMs} class="select">
        <option value={0}>off</option>
        <option value={5000}>5s</option>
        <option value={15000}>15s</option>
        <option value={60000}>60s</option>
      </select>
    </div>
  </section>

  <section class="filters">
    <span class="filters-label dim">Filter</span>
    <TagFilter
      bind:visible
      counts={aggCounts}
      {disabledIds}
      disabledHint="Disabled in minute granularity (perf). Switch to hour to enable."
    />
  </section>

  <section class="panel">
    <div class="panel-head">
      <h2>All events · per {granularity}</h2>
      <div class="head-tools">
        <button
          class="mode-btn"
          onclick={toggleGranularity}
          title="Toggle granularity (minute / hour). Minute is errors-only for performance."
          aria-label="Granularity: {granularity}"
        >
          {#if granularity === 'minute'}
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="9"/><path d="M12 7 V 12 L 15.5 14"/>
            </svg>
            <span>Minute</span>
          {:else}
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="9"/><path d="M12 7 V 12 L 16 12"/>
            </svg>
            <span>Hour</span>
          {/if}
        </button>
        <button
          class="mode-btn"
          onclick={toggleChartMode}
          title="Toggle chart mode (smooth / step)"
          aria-label="Chart mode: {chartMode}"
        >
          {#if chartMode === 'smooth'}
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M3 17 C 7 17, 8 7, 12 7 S 17 17, 21 17"/>
            </svg>
            <span>Smooth</span>
          {:else}
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M3 17 H 8 V 11 H 13 V 17 H 18 V 7 H 21"/>
            </svg>
            <span>Step</span>
          {/if}
        </button>
        <span class="dim">{globalBuckets.length} buckets · {bucketSec}s</span>
      </div>
    </div>
    <LineChart buckets={globalBuckets} {bucketSec} tags={TAGS} {visible} mode={chartMode} height={260} showLegend={true} />
  </section>

  <section class="grid">
    {#if loading && summary.length === 0}
      {#each Array(4) as _}
        <div class="card-skel">
          <div class="skel" style="height:14px;width:60%"></div>
          <div class="skel" style="height:10px;width:40%;margin-top:10px"></div>
          <div class="skel" style="height:130px;margin-top:14px"></div>
        </div>
      {/each}
    {:else if summary.length === 0}
      <div class="empty">
        {#if clusters.length === 0}
          <h3>No Sashimono instances detected yet.</h3>
          <p class="dim">Confirm <code>sashi list</code> works on this VM, or click <b>Discover</b> above.</p>
        {:else if monitoredClusters.length === 0}
          <h3>No clusters are being monitored.</h3>
          <p class="dim">Toggle a cluster on above to start tailing its instances. {clusters.length} cluster{clusters.length === 1 ? '' : 's'} available.</p>
        {:else}
          <h3>No events yet for the selected scope.</h3>
          <p class="dim">Monitored clusters are still warming up; events appear within ~30 s.</p>
        {/if}
      </div>
    {:else}
      {#each summary as inst (inst.name)}
        {@const cid = instanceCidMap.get(inst.name)}
        <InstanceCard
          instance={inst}
          buckets={perInstanceBuckets[inst.name] || []}
          spells={perInstanceSpells[inst.name] || []}
          {bucketSec}
          {visible}
          mode={chartMode}
          onSelect={(i) => selected = i}
          canDelete={cid != null && hardForkCids.has(cid)}
          contractId={cid}
          onDelete={askDelete}
        />
      {/each}
    {/if}
  </section>

  <footer class="ftr dim">
    <a href="https://github.com/du1ana/sashi-monitor" target="_blank" rel="noopener">github.com/du1ana/sashi-monitor</a>
    <span class="sep"></span>
    <span>API: <code>/api/summary</code>, <code>/api/histogram</code>, <code>/api/events</code></span>
  </footer>
</div>

{#if selected}
  <EventDrawer instance={selected} window={windowVal} onClose={() => selected = null} />
{/if}

{#if delTarget}
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="delTitle">
    <button class="modal-bg" onclick={closeDelete} aria-label="Close"></button>
    <div class="modal-box">
      <header class="modal-h">
        <h2 id="delTitle">Delete instance</h2>
        <button class="x" onclick={closeDelete} disabled={delBusy} aria-label="Close">×</button>
      </header>
      <div class="modal-body">
        <div class="row"><b>instance</b><code>{delTarget.name}</code></div>
        <div class="row"><b>cluster</b><code>{delTarget.contract_id || '?'}</code></div>
        <p>This will run <code>evernode delete {delTarget.name}</code> on the host and then
        re-run <code>sashi list</code> to confirm. The daemon only allows this for clusters
        currently in a hard-fork state. The action cannot be undone.</p>
        {#if delResult}
          <div class="status-line" class:ok={delResult.status === 'ok'} class:bad={delResult.status === 'bad'}>
            {delResult.text}
          </div>
          {#if delResult.transcript}
            <pre>{delResult.transcript}</pre>
          {/if}
        {/if}
        <div class="modal-actions">
          <button class="cancel" onclick={closeDelete} disabled={delBusy}>cancel</button>
          <button class="go danger" onclick={confirmDelete} disabled={delBusy || (delResult?.status === 'ok')}>
            {delBusy ? 'deleting…' : (delResult?.status === 'ok' ? 'done' : 'delete')}
          </button>
        </div>
      </div>
    </div>
  </div>
{/if}

{#if updOpen}
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="updTitle">
    <button class="modal-bg" onclick={closeUpdate} aria-label="Close"></button>
    <div class="modal-box">
      <header class="modal-h">
        <h2 id="updTitle">Update sashi.mon</h2>
        <button class="x" onclick={closeUpdate} disabled={updBusy} aria-label="Close">×</button>
      </header>
      <div class="modal-body">
        <p>Re-run the install one-liner on this host:</p>
        <pre>curl -fsSL &lt;install-url&gt; | sudo bash</pre>
        <p>systemd will restart sashimon when the install completes. The dashboard
        will reload itself once <code>/healthz</code> answers again (usually ~30–60s).</p>
        {#if updStatus}
          <div class="status-line" class:ok={updStatus.includes('reloading')} class:bad={updStatus.includes('failed') || updStatus.includes('did not')}>{updStatus}</div>
        {/if}
        <div class="modal-actions">
          <button class="cancel" onclick={closeUpdate} disabled={updBusy}>cancel</button>
          <button class="go ok-bg" onclick={confirmUpdate} disabled={updBusy}>
            {updBusy ? 'updating…' : 'update now'}
          </button>
        </div>
      </div>
    </div>
  </div>
{/if}

<style>
  .app {
    max-width: 1480px;
    margin: 0 auto;
    padding: 20px clamp(16px, 3vw, 32px) 60px;
  }

  /* ---- header ---- */
  .hdr {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 0 22px;
    gap: 16px; flex-wrap: wrap;
  }
  .brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
  .name {
    font-size: 18px; font-weight: 700; letter-spacing: -0.01em;
  }
  .tag { font-size: 12px; }
  .hdr-meta { font-size: 11px; display: flex; align-items: center; gap: 10px; }
  .err-pill {
    background: rgba(255,93,108,0.18);
    color: #ff97a2;
    font-size: 10px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.08em;
    padding: 2px 8px; border-radius: 999px;
  }
  .danger-btn {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 5px 10px;
    background: var(--bg-2);
    border: 1px solid var(--line);
    border-radius: 9px;
    color: var(--fg-muted);
    font-size: 11px; font-weight: 600;
    cursor: pointer;
    transition: color .15s, border-color .15s, background .15s, transform .12s;
  }
  .danger-btn:hover:not(:disabled) {
    color: #ff97a2;
    border-color: var(--red);
    background: rgba(255,93,108,0.08);
  }
  .danger-btn:hover:not(:disabled) svg { color: var(--red); }
  .danger-btn:active:not(:disabled) { transform: scale(0.97); }
  .danger-btn:disabled { opacity: 0.55; cursor: progress; }
  .danger-btn svg { color: var(--fg-dim); }
  .db-size {
    margin-left: 2px;
    font-variant-numeric: tabular-nums;
    font-weight: 500;
    color: var(--fg-dim);
    font-size: 10.5px;
    letter-spacing: 0.01em;
  }
  .danger-btn:hover:not(:disabled) .db-size { color: #ff97a2; opacity: 0.85; }

  .policy {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 8px 4px 10px;
    background: var(--bg-2);
    border: 1px solid var(--line);
    border-radius: 9px;
    font-size: 10.5px;
    color: var(--fg-muted);
    transition: border-color .15s, background .15s;
  }
  .policy:hover { border-color: var(--line-2, var(--line)); }
  .policy-k {
    font-size: 9.5px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--fg-dim);
  }
  .policy-sel {
    background: transparent;
    color: var(--fg);
    border: 0;
    padding: 1px 4px 1px 0;
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
    outline: none;
    font-family: inherit;
  }
  .policy-sel:disabled { opacity: 0.55; cursor: progress; }
  .policy-sel option { background: var(--bg-1); color: var(--fg); }

  /* ---- active-cluster banner ---- */
  .cluster-banner {
    display: flex; align-items: center; gap: 12px;
    padding: 9px 14px; margin-bottom: 14px;
    background: color-mix(in srgb, var(--green) 8%, var(--bg-1));
    border: 1px solid color-mix(in srgb, var(--green) 35%, var(--line));
    border-radius: var(--radius);
    flex-wrap: wrap;
  }
  .cb-label {
    font-size: 9.5px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--fg-dim);
  }
  .cb-id {
    font-family: var(--mono); font-size: 11px;
    background: var(--bg-3);
    padding: 2px 8px; border-radius: 4px;
    color: var(--fg); font-weight: 600;
    word-break: break-all;
  }
  .cb-meta { font-size: 11px; font-variant-numeric: tabular-nums; }
  .cb-clear {
    margin-left: auto;
    background: transparent;
    border: 1px solid var(--line);
    color: var(--fg-muted);
    border-radius: 9px;
    font-size: 10.5px; font-weight: 600;
    padding: 3px 10px;
    cursor: pointer;
    transition: color .15s, border-color .15s;
  }
  .cb-clear:hover { color: var(--fg); border-color: var(--fg-dim); }

  /* ---- update button ---- */
  .upd-btn {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 5px 10px;
    background: color-mix(in srgb, var(--green) 10%, var(--bg-2));
    border: 1px solid color-mix(in srgb, var(--green) 35%, var(--line));
    border-radius: 9px;
    color: var(--fg);
    font-size: 11px; font-weight: 600;
    cursor: pointer;
    transition: background .15s, border-color .15s, transform .12s;
  }
  .upd-btn:hover {
    background: color-mix(in srgb, var(--green) 22%, var(--bg-2));
    border-color: var(--green);
  }
  .upd-btn:active { transform: scale(0.97); }
  .upd-btn svg { color: var(--green); }

  /* ---- modal (shared) ---- */
  .modal {
    position: fixed; inset: 0; z-index: 200;
    display: flex; align-items: center; justify-content: center;
  }
  .modal .modal-bg {
    position: absolute; inset: 0;
    background: rgba(0,0,0,0.62);
    backdrop-filter: blur(2px);
    border: 0; cursor: pointer;
  }
  .modal .modal-box {
    position: relative;
    width: min(640px, 94vw);
    max-height: 86vh; overflow: auto;
    background: var(--bg-1);
    border: 1px solid var(--line-2, var(--line));
    border-radius: 10px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.55);
  }
  .modal .modal-h {
    display: flex; align-items: center;
    padding: 12px 16px;
    border-bottom: 1px solid var(--line);
    gap: 10px;
  }
  .modal .modal-h h2 {
    margin: 0; font-size: 12px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--fg);
  }
  .modal .modal-h .x {
    margin-left: auto;
    width: 26px; height: 26px;
    background: transparent; border: 1px solid var(--line);
    color: var(--fg-dim); border-radius: 4px;
    cursor: pointer; line-height: 1; font-size: 14px;
  }
  .modal .modal-h .x:hover:not(:disabled) { color: var(--fg); border-color: var(--fg-dim); }
  .modal-body { padding: 14px 16px 16px; font-size: 12px; line-height: 1.55; }
  .modal-body p { margin: 8px 0; }
  .modal-body .row {
    display: flex; gap: 10px; align-items: center;
    margin-bottom: 8px;
    font-size: 11.5px;
  }
  .modal-body .row b {
    font-size: 9.5px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--fg-dim);
  }
  .modal-body code {
    background: var(--bg-3);
    padding: 1px 6px; border-radius: 4px;
    font-family: var(--mono); font-size: 11px;
    color: var(--fg);
  }
  .modal-body pre {
    background: var(--bg-3);
    padding: 10px 12px;
    border: 1px solid var(--line);
    border-radius: 6px;
    max-height: 320px; overflow: auto;
    font-family: var(--mono); font-size: 10.5px;
    white-space: pre-wrap; word-break: break-word;
    margin: 8px 0;
  }
  .modal-body .status-line {
    margin-top: 10px; font-size: 11.5px;
    color: var(--amber);
  }
  .modal-body .status-line.ok  { color: var(--green); }
  .modal-body .status-line.bad { color: var(--red); }
  .modal-actions {
    display: flex; gap: 8px; justify-content: flex-end;
    margin-top: 14px;
  }
  .modal-actions button {
    padding: 6px 14px; border-radius: 8px;
    font-size: 11px; font-weight: 600;
    cursor: pointer;
  }
  .modal-actions .cancel {
    background: var(--bg-2);
    border: 1px solid var(--line);
    color: var(--fg-muted);
  }
  .modal-actions .cancel:hover:not(:disabled) { color: var(--fg); border-color: var(--fg-dim); }
  .modal-actions .go.danger {
    background: color-mix(in srgb, var(--red) 28%, var(--bg-2));
    border: 1px solid var(--red);
    color: #fff;
  }
  .modal-actions .go.danger:hover:not(:disabled) {
    background: color-mix(in srgb, var(--red) 45%, var(--bg-2));
  }
  .modal-actions .go.ok-bg {
    background: color-mix(in srgb, var(--green) 28%, var(--bg-2));
    border: 1px solid var(--green);
    color: #fff;
  }
  .modal-actions .go.ok-bg:hover:not(:disabled) {
    background: color-mix(in srgb, var(--green) 45%, var(--bg-2));
  }
  .modal-actions button:disabled { opacity: 0.55; cursor: progress; }

  /* ---- KPIs ---- */
  .kpis {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 12px;
    margin-bottom: 18px;
  }
  .kpi {
    background: linear-gradient(180deg, var(--bg-1), var(--bg-2));
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 14px 16px;
    display: flex; flex-direction: column; gap: 6px;
    min-width: 0;
    box-shadow: var(--shadow-1);
  }
  .kpi.window { gap: 10px; }
  .k {
    font-size: 10px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.08em;
    color: var(--fg-dim);
  }
  .k .muted { color: var(--fg-dim); font-weight: 500; text-transform: none; letter-spacing: 0; }
  .v {
    font-size: 24px; font-weight: 700;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.02em;
  }
  .v.bad { color: var(--red); }
  .health-row { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 2px; }
  .hbit {
    font-size: 10px; font-weight: 700;
    padding: 1px 7px; border-radius: 999px;
    font-variant-numeric: tabular-nums;
  }
  .hbit-healthy        { background: rgba(0,214,143,.16);  color: #69f0bd; }
  .hbit-consensus_loss { background: rgba(245,177,74,.16); color: #ffd28a; }
  .hbit-forked         { background: rgba(255,93,108,.18); color: #ff97a2; }
  .hbit-stalled        { background: rgba(177,140,255,.18);color: #d4baff; }
  .hbit-unknown        { background: rgba(91,102,122,.25); color: #aab4c2; }

  .select {
    background: var(--bg-2);
    color: var(--fg);
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 6px 10px;
    font-size: 12px; font-weight: 600;
    width: max-content;
  }

  /* ---- filters ---- */
  .filters {
    display: flex; align-items: center; gap: 10px;
    margin-bottom: 18px;
    flex-wrap: wrap;
  }
  .filters-label {
    font-size: 10px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.08em;
  }

  /* ---- main panel ---- */
  .panel {
    background: linear-gradient(180deg, var(--bg-1), var(--bg-2));
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 18px;
    margin-bottom: 22px;
    box-shadow: var(--shadow-1);
  }
  .panel-head {
    display: flex; align-items: baseline; justify-content: space-between;
    margin-bottom: 8px;
    gap: 12px; flex-wrap: wrap;
  }
  .panel-head h2 {
    margin: 0; font-size: 12px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.08em; color: var(--fg-muted);
  }
  .panel-head .dim { font-size: 10.5px; }
  .head-tools { display: flex; align-items: center; gap: 12px; }
  .mode-btn {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 5px 10px;
    background: var(--bg-2);
    border: 1px solid var(--line);
    border-radius: 9px;
    color: var(--fg-muted);
    font-size: 11px; font-weight: 600;
    transition: color .15s, border-color .15s, background .15s, transform .12s;
  }
  .mode-btn:hover {
    color: var(--fg); border-color: var(--green);
    background: color-mix(in srgb, var(--green) 8%, var(--bg-2));
  }
  .mode-btn:active { transform: scale(0.97); }
  .mode-btn svg { color: var(--green); }

  /* ---- instance grid ---- */
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
    gap: 14px;
  }
  .card-skel {
    background: var(--bg-1);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    padding: 16px;
  }
  .empty {
    grid-column: 1 / -1;
    text-align: center;
    padding: 60px 20px;
    border: 1px dashed var(--line);
    border-radius: var(--radius);
  }
  .empty h3 { margin: 0 0 6px; font-size: 14px; }
  .empty code {
    background: var(--bg-3); padding: 2px 6px; border-radius: 4px;
    font-family: var(--mono); font-size: 12px;
  }

  /* ---- footer ---- */
  .ftr {
    margin-top: 32px;
    padding: 16px 0;
    border-top: 1px solid var(--line);
    font-size: 11px;
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
  }
  .ftr a { color: var(--fg-muted); text-decoration: none; }
  .ftr a:hover { color: var(--fg); }
  .ftr .sep { width: 4px; height: 4px; background: var(--line-2); border-radius: 50%; }
  .ftr code { font-family: var(--mono); font-size: 10.5px; color: var(--fg-muted); }

  /* ---- responsive ---- */
  @media (max-width: 1100px) {
    .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .kpi.window:last-child { grid-column: span 2; }
  }
  @media (max-width: 600px) {
    .kpis { grid-template-columns: 1fr; }
    .kpi.window:last-child { grid-column: 1; }
    .panel { padding: 14px; }
    .v { font-size: 20px; }
  }
</style>
