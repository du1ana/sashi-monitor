<script>
  import { onMount } from 'svelte';
  import { getJSON, incMeta, clock } from './util.js';
  let { incident, onclose } = $props();

  let full = $state(null);
  let error = $state(null);
  let filter = $state('');
  let important = $state(true);

  const KEY = /Ledger created|Cannot close|Not enough|No consensus|not on the consensus|Winning proposal|Peer proved|Missed stage|Skipped|Consensus stats|Contract config|Applying pending|certificate|\[err\]|\[wrn\]/;

  onMount(async () => {
    try {
      full = await getJSON(`api/incident?host=${encodeURIComponent(incident.host)}&id=${incident.id}`);
      if (full.error) { error = full.error; full = null; }
    } catch (e) { error = String(e.message || e); }
    const onKey = (e) => e.key === 'Escape' && onclose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  // Start each captured log at its end: the incident happened at the end of the capture.
  function toEnd(el) { requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; }); }

  function lines(text) {
    let ls = text.split('\n');
    if (important) ls = ls.filter((l) => KEY.test(l));
    if (filter) ls = ls.filter((l) => l.toLowerCase().includes(filter.toLowerCase()));
    return ls.slice(-1500);
  }
  function cls(l) {
    if (/\[err\]|Conflicting|Invalid ledger/.test(l)) return 'e';
    if (/\[wrn\]|Winning proposal/.test(l)) return 'w';
    if (/Ledger created/.test(l)) return 'ok';
    if (/Peer proved/.test(l)) return 'i';
    if (/Cannot close|No consensus|Not enough|not on the consensus/.test(l)) return 'w2';
    return '';
  }
  const m = $derived(incMeta(incident.kind));
</script>

<div class="backdrop" onclick={onclose} role="presentation"></div>
<aside class="drawer panel" aria-label="Incident detail">
  <div class="panel-head">
    <div class="hd">
      <span class="pill {m.cls}">{m.icon} {m.label}</span>
      <span class="mono">{incident.host}{incident.node ? ' / ' + incident.node : ''}</span>
      <span class="faint mono">{clock(incident.start_ts)} → {incident.end_ts ? clock(incident.end_ts) : 'open'}</span>
    </div>
    <button class="btn" onclick={onclose}>Close</button>
  </div>
  <div class="panel-body body">
    <p class="summary">{incident.summary}</p>
    {#if incident.detail && Object.keys(incident.detail).length}
      <details open={incident.kind === 'fork' || incident.kind === 'stall' || incident.kind === 'height_split'}>
        <summary>Detail</summary>
        <pre class="mono">{JSON.stringify(incident.detail, null, 2)}</pre>
      </details>
    {/if}
    {#if error}<p class="faint">Could not load captures: {error}</p>{/if}
    {#if full}
      {#each full.captures.filter((c) => c.kind === 'proc') as c}
        <div class="proc" class:blocked={c.json?.blocked_in_stdout_write}>
          <b>{c.json?.blocked_in_stdout_write ? 'hpcore is blocked writing to stdout' : 'hpcore thread states'}</b>
          <span class="faint">stdout → {c.json?.stdout} · pid {c.json?.pid}</span>
          <table class="t mono">
            <thead><tr><th>tid</th><th>state</th><th>wchan</th><th>syscall</th><th>arg0</th></tr></thead>
            <tbody>{#each c.json?.threads || [] as t}<tr class:hl={t.syscall === '1' && t.arg0 === '0x1'}><td>{t.tid}</td><td>{t.state}</td><td>{t.wchan}</td><td>{t.syscall}</td><td>{t.arg0}</td></tr>{/each}</tbody>
          </table>
        </div>
      {/each}
      {@const logs = full.captures.filter((c) => c.kind === 'log')}
      {#if logs.length}
        <div class="logbar">
          <b>Captured log</b>
          <label><input type="checkbox" bind:checked={important} /> key lines only</label>
          <input class="search" placeholder="filter…" bind:value={filter} />
        </div>
        {#each logs as c}
          <details open={logs.length === 1}>
            <summary class="mono">{c.node} · captured {clock(c.ts)} · {c.text.split('\n').length} lines</summary>
            <div class="log mono" use:toEnd>{#each lines(c.text) as l}<div class={cls(l)}>{l}</div>{/each}</div>
          </details>
        {/each}
      {:else}
        <p class="faint">No log capture for this event (captures are saved for freezes, stalls, splits, forks and refused closes).</p>
      {/if}
    {/if}
  </div>
</aside>

<style>
  .backdrop { position: fixed; inset: 0; background: rgba(0, 0, 0, .35); z-index: 40; }
  .drawer { position: fixed; top: 0; right: 0; bottom: 0; width: min(980px, 100vw); z-index: 41; border-radius: 0;
    display: flex; flex-direction: column; }
  .hd { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .body { overflow: auto; display: grid; gap: 14px; align-content: start; }
  .summary { margin: 0; font-size: 15px; font-weight: 600; }
  pre { background: var(--panel-2); padding: 10px 12px; border-radius: 8px; overflow: auto; max-height: 280px; margin: 8px 0 0; }
  summary { cursor: pointer; font-weight: 600; font-size: 13px; }
  .proc { padding: 12px; border-radius: 8px; background: var(--panel-2); display: grid; gap: 6px; }
  .proc.blocked { background: var(--violet-bg); }
  .proc.blocked b { color: var(--violet); }
  tr.hl td { background: var(--violet-bg); color: var(--violet); font-weight: 700; }
  .logbar { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; font-size: 13px; }
  .search { border: 1px solid var(--border); background: var(--panel); border-radius: 6px; padding: 4px 8px; color: var(--text); }
  .log { background: var(--panel-2); border-radius: 8px; padding: 8px 10px; max-height: 60vh; overflow: auto; font-size: 11.5px;
    white-space: pre; margin-top: 8px; }
  .log .e { color: var(--bad); } .log .w { color: var(--warn); } .log .w2 { color: var(--warn); opacity: .85; }
  .log .ok { color: var(--ok); } .log .i { color: var(--info); }
</style>
