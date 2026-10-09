<script>
  let { hosts, error } = $props();

  const level = (v, warn, bad) => (v == null ? 'neutral' : v >= bad ? 'bad' : v >= warn ? 'warn' : 'ok');
  const fmt = (v, d = 0) => (v == null ? '—' : Number(v).toFixed(d));
</script>

<section class="strip">
  {#each hosts as h}
    {@const m = h.metrics || {}}
    {@const loadPerCpu = m.load && m.cpus ? m.load[0] / m.cpus : null}
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
          <div class="m"><span class="k">sashimon</span><span class="v num">{fmt(m.db_mb, 1)} MB</span>
            <span class="s faint num">cpu {fmt(m.sashimon_cpu, 1)}% · {h.nodes} nodes</span></div>
        </div>
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
  @media (max-width: 640px) {
    .strip { grid-template-columns: 1fr; }
    .row { grid-template-columns: repeat(3, 1fr); }
  }
</style>
