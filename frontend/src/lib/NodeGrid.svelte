<script>
  import { ago } from './util.js';
  let { live, historyNodes, stats } = $props();

  const maxSeq = $derived(Math.max(0, ...live.map((n) => n.seq || 0)));
  const hosts = $derived([...new Set(live.map((n) => n.host))].sort());
  const latestStats = $derived.by(() => {
    const m = {};
    for (const s of stats) if (!m[s.node] || m[s.node].ts < s.ts) m[s.node] = s;
    return m;
  });
  let sel = $state(null);

  function nodeState(n) {
    if (n.frozen_since) return { cls: 'violet', label: 'frozen' };
    if (n.idle_s == null || n.idle_s > 15) return { cls: 'bad', label: 'silent' };
    const lag = maxSeq - (n.seq || 0);
    if (lag > 5) return { cls: 'bad', label: `-${lag}` };
    if (lag > 1) return { cls: 'warn', label: `-${lag}` };
    if (n.vote_status === 'synced') return { cls: 'ok', label: 'synced' };
    if (n.vote_status) return { cls: 'warn', label: n.vote_status };
    return { cls: 'ok', label: 'live' };
  }
</script>

<section class="panel">
  <div class="panel-head">
    <h3>Nodes</h3>
    <div class="legend faint">
      <span class="pill ok">synced</span><span class="pill warn">behind / unreliable</span>
      <span class="pill bad">silent / far behind</span><span class="pill violet">frozen</span>
    </div>
  </div>
  <div class="panel-body hosts">
    {#if !live.length}<div class="empty">No live nodes: this cluster is offline. History below is from the monitor's database.</div>{/if}
    {#each hosts as host}
      {@const ns = live.filter((n) => n.host === host).sort((a, b) => (a.short > b.short ? 1 : -1))}
      <div class="hostcol">
        <div class="hostname muted">{host} <span class="faint">· {ns.length}</span></div>
        <div class="nodes">
          {#each ns as n (n.name)}
            {@const s = nodeState(n)}
            <button class="node {s.cls}" class:sel={sel === n.name} onclick={() => (sel = sel === n.name ? null : n.name)}
              title={`${n.name}\nheight ${n.seq ?? '?'} · ${n.vote_status ?? '?'} · ${n.last_votes ?? ''}`}>
              <span class="id mono">{n.short}</span>
              <span class="h num">{n.seq ?? '—'}</span>
              <span class="st">{s.label}</span>
            </button>
          {/each}
        </div>
      </div>
    {/each}
  </div>
  {#if sel}
    {@const n = live.find((x) => x.name === sel)}
    {#if n}
      {@const st = latestStats[n.short]}
      <div class="detail">
        <div class="dgrid">
          <div><span class="k">Node</span><span class="mono">{n.name}</span></div>
          <div><span class="k">Host</span>{n.host}</div>
          <div><span class="k">Version</span>{n.version ?? '?'}</div>
          <div><span class="k">Pubkey</span><span class="mono">{n.pubkey ? n.pubkey.slice(0, 18) + '…' : '?'}</span></div>
          <div><span class="k">Ledger</span><span class="mono">{n.seq ?? '—'}-{n.hash ?? ''}</span></div>
          <div><span class="k">Vote status</span>{n.vote_status ?? '?'} {n.last_votes ? `· ${n.last_votes}` : ''}</div>
          <div><span class="k">Last log line</span>{ago(n.idle_s)} ago</div>
          <div><span class="k">Image</span><span class="mono">{n.image ?? ''}</span></div>
        </div>
        {#if n.proc}
          <div class="proc" class:blocked={n.proc.blocked_in_stdout_write}>
            {n.proc.blocked_in_stdout_write ? 'Blocked writing to stdout (' + (n.proc.stdout || '?') + '): the log reader is not draining output.' : 'Process diagnosis'}
            <span class="faint mono">pid {n.proc.pid} · {(n.proc.threads || []).length} threads</span>
          </div>
        {/if}
        {#if st}
          <div class="stats">
            <span class="k">HotPocket stats (last {st.rounds} rounds, {ago(Date.now() / 1000 - st.ts)} ago)</span>
            <div class="chips">
              {#each Object.entries(st.data) as [k, v]}<span class="chip"><span class="muted">{k}</span> <b class="num">{v}</b></span>{/each}
            </div>
          </div>
        {/if}
      </div>
    {/if}
  {/if}
</section>

<style>
  .legend { display: flex; gap: 6px; flex-wrap: wrap; }
  .legend .pill { font-weight: 500; font-size: 11px; }
  .hosts { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 18px; }
  .hostname { font-size: 12px; font-weight: 600; margin-bottom: 8px; }
  .nodes { display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 8px; }
  .node {
    display: grid; gap: 1px; text-align: left; padding: 8px 10px; border-radius: 8px; cursor: pointer;
    border: 1px solid var(--border); background: var(--panel-2); border-left: 3px solid var(--faint);
  }
  .node.ok { border-left-color: var(--ok); }
  .node.warn { border-left-color: var(--warn); background: var(--warn-bg); }
  .node.bad { border-left-color: var(--bad); background: var(--bad-bg); }
  .node.violet { border-left-color: var(--violet); background: var(--violet-bg); }
  .node.sel { outline: 2px solid var(--accent); outline-offset: 1px; }
  .id { font-size: 12px; color: var(--muted); }
  .h { font-size: 16px; font-weight: 650; }
  .st { font-size: 11px; color: var(--muted); }
  .detail { border-top: 1px solid var(--border); padding: 14px 16px; display: grid; gap: 12px; }
  .dgrid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 8px 18px; font-size: 13px; }
  .dgrid > div { display: flex; flex-direction: column; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  .k { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
  .proc { padding: 10px 12px; border-radius: 8px; background: var(--panel-2); font-size: 13px; display: flex; gap: 10px; justify-content: space-between; flex-wrap: wrap; }
  .proc.blocked { background: var(--violet-bg); color: var(--violet); font-weight: 600; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
  .chip { background: var(--panel-2); border: 1px solid var(--border); border-radius: 6px; padding: 2px 8px; font-size: 12px; }
</style>
