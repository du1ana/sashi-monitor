<script>
  import { ago, clusterHealth, versionClass, incMeta, short } from './util.js';
  let { overview } = $props();

  let picked = $state([]);
  function toggle(cid) {
    picked = picked.includes(cid) ? picked.filter((x) => x !== cid) : [...picked.slice(-1), cid];
  }
  const compareHref = $derived(picked.length === 2
    ? `#/compare/${encodeURIComponent(picked[0])}/${encodeURIComponent(picked[1])}` : null);
</script>

<section class="head">
  <div>
    <h2>Clusters</h2>
    <p class="muted">Every HotPocket cluster with nodes on the monitored hosts. Pick two to compare (e.g. 0.6.5 vs 0.6.6).</p>
  </div>
  <a class="btn primary" class:disabled={!compareHref} href={compareHref || undefined}
     aria-disabled={!compareHref}>Compare {picked.length}/2</a>
</section>

{#if !overview.clusters.length}
  <div class="panel empty">No HotPocket instances found yet. Discovery runs every 15 s.</div>
{:else}
  <section class="cards">
    {#each overview.clusters as c (c.contract_id)}
      {@const h = clusterHealth(c)}
      {@const spread = c.max_seq != null && c.min_seq != null ? c.max_seq - c.min_seq : null}
      <article class="panel card" class:picked={picked.includes(c.contract_id)} class:offline={c.offline}>
        <a class="body" href={`#/c/${encodeURIComponent(c.contract_id)}`}>
          <div class="row1">
            <span class="pill {h.cls}"><span class="dot"></span>{h.label}</span>
            <span class="pill {versionClass(c.version)}">HotPocket {c.version}</span>
          </div>
          <div class="cid mono" title={c.contract_id}>{short(c.contract_id, 18)}…</div>
          <div class="kpis">
            <div class="kpi"><span class="label">Height</span><span class="value">{c.max_seq ?? '—'}</span></div>
            <div class="kpi"><span class="label">Spread</span>
              <span class="value" class:warnv={spread > 3}>{spread ?? '—'}</span></div>
            <div class="kpi"><span class="label">Last close</span>
              <span class="value" class:badv={c.last_close_age > 60}>{ago(c.last_close_age)}</span></div>
          </div>
          <div class="meta">
            <span>{c.offline ? 'no live nodes' : `${c.nodes} nodes`}</span>
            {#each Object.entries(c.hosts) as [host, n]}<span class="faint">· {host} {n}</span>{/each}
            {#if c.frozen}<span class="pill violet">❄ {c.frozen} frozen</span>{/if}
            {#if c.silent && !c.frozen}<span class="pill warn">{c.silent} silent</span>{/if}
          </div>
          {#if c.open_incidents.length}
            <div class="incs">
              {#each c.open_incidents as i}
                {@const m = incMeta(i.kind)}
                <span class="pill {m.cls}">{m.icon} {m.label}{i.key ? ` ${i.key}` : ''}</span>
              {/each}
            </div>
          {/if}
        </a>
        <label class="pick">
          <input type="checkbox" checked={picked.includes(c.contract_id)} onchange={() => toggle(c.contract_id)} />
          compare
        </label>
      </article>
    {/each}
  </section>
{/if}

<style>
  .head { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; }
  h2 { margin: 0 0 2px; font-size: 18px; }
  .head p { margin: 0; font-size: 13px; }
  .btn.disabled { opacity: .45; pointer-events: none; }
  .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 14px; }
  .card { position: relative; transition: border-color .15s; }
  .card:hover { border-color: var(--faint); }
  .card.offline { opacity: .78; }
  .card.picked { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
  .body { display: grid; gap: 12px; padding: 16px 16px 40px; color: var(--text); }
  .row1 { display: flex; gap: 8px; flex-wrap: wrap; }
  .cid { color: var(--muted); }
  .kpis { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
  .warnv { color: var(--warn); } .badv { color: var(--bad); }
  .meta { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; font-size: 12.5px; color: var(--muted); }
  .incs { display: flex; flex-wrap: wrap; gap: 6px; }
  .pick { position: absolute; bottom: 12px; right: 16px; font-size: 12px; color: var(--muted); display: flex; gap: 5px;
    align-items: center; cursor: pointer; }
  @media (max-width: 640px) { .cards { grid-template-columns: 1fr; } .head { flex-direction: column; align-items: stretch; } }
</style>
