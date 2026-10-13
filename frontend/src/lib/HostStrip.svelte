<script>
  import { ago, clusterColor, until, short } from './util.js';
  let { hosts, error, clusters = [], nodeState = {} } = $props();

  const level = (v, warn, bad) => (v == null ? 'neutral' : v >= bad ? 'bad' : v >= warn ? 'warn' : 'ok');
  const fmt = (v, d = 0) => (v == null ? '—' : Number(v).toFixed(d));

  // Instance boxes for one host: cluster nodes grouped by cluster, then other tenants, then placeholders, then free.
  function slots(h) {
    const s = h.slots || {};
    const inst = [...(s.instances || [])];
    const rank = { node: 0, other: 1, placeholder: 2 };
    inst.sort((a, b) => (rank[a.kind] - rank[b.kind]) || String(a.contract_id).localeCompare(String(b.contract_id)) ||
      String(a.name).localeCompare(String(b.name)));
    const free = s.max != null ? Math.max(0, s.max - inst.length) : 0;
    return { inst, free, max: s.max };
  }

  // Health of a node box: stalled cluster or node far behind / silent / frozen gets a red edge.
  function boxState(i) {
    if (i.kind !== 'node') return '';
    const n = nodeState[i.name];
    const c = clusters.find((x) => x.contract_id === i.contract_id);
    if (!n) return 'bad';
    if (n.frozen || n.idle_s == null || n.idle_s > 15) return 'bad';
    if (c?.last_close_age != null && c.last_close_age > 60) return 'bad';
    if (c?.max_seq != null && n.seq != null && c.max_seq - n.seq > 5) return 'bad';
    if (c?.max_seq != null && n.seq != null && c.max_seq - n.seq > 1) return 'warn';
    return 'ok';
  }

  function tip(i) {
    const exp = until(i.expiry_ts);
    const lines = [i.name];
    if (i.kind === 'node') {
      const n = nodeState[i.name] || {};
      lines.push(`cluster ${short(i.contract_id, 8)} · HotPocket ${n.version ?? '?'}`);
      lines.push(`height ${n.seq ?? '?'} · ${n.vote ?? '?'}`);
    } else if (i.kind === 'placeholder') {
      lines.push('placeholder (holds a slot, does nothing)');
    } else {
      lines.push(`other tenant ${i.tenant ? short(i.tenant, 10) : '?'} · not HotPocket-tracked`);
    }
    lines.push(`image ${i.image || '?'}`);
    lines.push(exp == null ? 'expiry unknown' : exp > 0 ? `expires in ${ago(exp)}` : 'expired');
    return lines.join('\n');
  }

  function open(i) {
    if (i.kind === 'node') location.hash = `#/c/${encodeURIComponent(i.contract_id)}`;
  }
</script>

<section class="strip">
  {#each hosts as h}
    {@const m = h.metrics || {}}
    {@const loadPerCpu = m.load && m.cpus ? m.load[0] / m.cpus : null}
    {@const sl = slots(h)}
    <div class="host panel">
      <div class="top">
        <span class="pill {h.ok ? 'ok' : 'bad'}"><span class="dot"></span>{h.host}</span>
        <span class="faint">{h.self ? 'this host' : h.ok ? `peer · ${fmt(h.age)}s ago` : 'peer unreachable'}</span>
      </div>
      {#if h.ok && h.metrics}
        <div class="row">
          <div class="m" title="1-minute load average per CPU">
            <span class="k">load / cpu</span>
            <span class="v num {level(loadPerCpu, 2, 8)}">{fmt(loadPerCpu, 1)}</span>
            <span class="s faint num">{fmt(m.load?.[0], 1)} on {m.cpus}</span>
          </div>
          <div class="m"><span class="k">cpu</span><span class="v num {level(m.cpu, 80, 95)}">{fmt(m.cpu)}%</span>
            <span class="s faint num">io {fmt(m.iowait)}%</span></div>
          <div class="m"><span class="k">memory</span><span class="v num {level(m.mem_pct, 85, 95)}">{fmt(m.mem_pct)}%</span>
            <span class="s faint num">swap {fmt(m.swap_mb)} MB</span></div>
          <div class="m"><span class="k">disk</span><span class="v num {level(m.disk_pct, 80, 92)}">{fmt(m.disk_pct)}%</span>
            <span class="s faint num">{fmt((m.disk_free_mb || 0) / 1024, 1)} GB free</span></div>
          <div class="m"><span class="k">sashi.mon</span><span class="v num">{fmt(m.db_mb, 1)} MB</span>
            <span class="s faint num">cpu {fmt(m.sashimon_cpu, 1)}% · {h.nodes} nodes</span></div>
        </div>
        {#if h.slots}
          <div class="slots-head">
            <span class="k">instance slots</span>
            <span class="faint">{sl.inst.length}{sl.max != null ? ` / ${sl.max}` : ''} used</span>
          </div>
          <div class="slots">
            {#each sl.inst as i (i.name)}
              {@const st = boxState(i)}
              <button class="slot {i.kind} {st}" title={tip(i)} onclick={() => open(i)}
                style={i.kind === 'node' ? `--c:${clusterColor(i.contract_id)}` : ''}>
                {#if i.kind === 'node'}<span class="num">{nodeState[i.name]?.seq ?? '—'}</span>{/if}
              </button>
            {/each}
            {#each Array(sl.free) as _, k (k)}
              <span class="slot free" title="free slot"></span>
            {/each}
          </div>
        {/if}
      {:else}
        <div class="err faint">{h.error || 'no data'}</div>
      {/if}
    </div>
  {/each}
  {#if error}<div class="panel host err">API: {error}</div>{/if}
</section>

<style>
  .strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 14px; }
  .host { padding: 12px 16px; display: grid; gap: 10px; }
  .top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .row { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; }
  .m { display: flex; flex-direction: column; min-width: 0; }
  .k { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }
  .v { font-size: 17px; font-weight: 650; }
  .v.ok { color: var(--text); } .v.warn { color: var(--warn); } .v.bad { color: var(--bad); }
  .s { font-size: 11.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .err { font-size: 12.5px; }
  .slots-head { display: flex; justify-content: space-between; align-items: baseline; font-size: 12px; }
  .slots { display: flex; flex-wrap: wrap; gap: 4px; }
  .slot {
    width: 42px; height: 24px; border-radius: 5px; border: 1px solid var(--border); padding: 0;
    display: inline-flex; align-items: center; justify-content: center; font-size: 10.5px; font-weight: 650;
    background: var(--panel-2); color: var(--text);
  }
  .slot.node { background: var(--c); border: 2px solid var(--c); color: #fff; cursor: pointer;
    text-shadow: 0 0 2px rgba(0, 0, 0, .55); }
  /* Health as an outline outside the box, so it stays visible whatever the cluster colour is. */
  .slot.node.warn { outline: 2px solid var(--warn); outline-offset: 1px; }
  .slot.node.bad { outline: 2px solid var(--bad); outline-offset: 1px; }
  .slot.placeholder { background: var(--faint); opacity: .55; cursor: default; }
  .slot.other { cursor: default; background: repeating-linear-gradient(45deg, var(--muted) 0 3px, transparent 3px 7px); opacity: .7; }
  .slot.free { background: transparent; border-style: dashed; }
  @media (max-width: 640px) {
    .strip { grid-template-columns: 1fr; }
    .row { grid-template-columns: repeat(3, 1fr); }
  }
</style>
