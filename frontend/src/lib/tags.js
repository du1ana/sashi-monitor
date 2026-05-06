export const TAGS = [
  { id: 'ledger_created',   label: 'Ledger',          color: 'var(--t-ledger)',    important: true },
  { id: 'contract_running', label: 'Running',         color: 'var(--t-running)' },
  { id: 'consensus_lost',   label: 'Consensus loss',  color: 'var(--t-cons-lost)', important: true },
  { id: 'fork_warn',        label: 'Fork warning',    color: 'var(--t-fork)',      important: true },
  { id: 'out_of_sync',      label: 'Out of sync',     color: 'var(--t-out-sync)',  important: true },
  { id: 'error',            label: 'Error',           color: 'var(--t-error)',     important: true },
  { id: 'warning',          label: 'Warning',         color: 'var(--t-warn)' },
  { id: 'hp_started',       label: 'HP started',      color: 'var(--t-started)' },
  { id: 'hp_stopped',       label: 'HP stopped',      color: 'var(--t-stopped)' },
  { id: 'role_change',      label: 'Role change',     color: 'var(--t-role)' },
  { id: 'info_other',       label: 'Other',           color: 'var(--t-other)' },
];

export const TAG_BY_ID = Object.fromEntries(TAGS.map(t => [t.id, t]));

export const HEALTH = {
  healthy:        { label: 'healthy',         bg: 'rgba(0,214,143,0.18)',  fg: '#69f0bd', dot: 'var(--green)' },
  consensus_loss: { label: 'consensus loss',  bg: 'rgba(245,177,74,0.16)', fg: '#ffd28a', dot: 'var(--amber)' },
  forked:         { label: 'forked',          bg: 'rgba(255,93,108,0.18)', fg: '#ff97a2', dot: 'var(--red)' },
  stalled:        { label: 'stalled',         bg: 'rgba(177,140,255,0.18)',fg: '#d4baff', dot: 'var(--purple)' },
  unknown:        { label: 'unknown',         bg: 'rgba(91,102,122,0.25)', fg: '#aab4c2', dot: 'var(--fg-dim)' },
};

export function cssVar(name) {
  if (typeof document === 'undefined') return '#888';
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#888';
}
