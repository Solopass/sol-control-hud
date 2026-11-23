// localhost ports (split from app.js)
'use strict';
// ---------- localhost: what listens on this PC, what closed, and closing a dev server you forgot
// The card shows dev servers and the AI stack by default; apps and Windows services are a click away (there are
// dozens of them and none of them is why you opened this). What may be closed is decided by the server, not here.
let portsAll = false;

function upWords(s) {
  if (s == null) return '';
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h`;
  return `${(s / 86400).toFixed(1)}d`;
}
function portWhen(epoch) {
  if (!epoch) return '';
  const d = new Date(epoch * 1000), hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return d.toDateString() === new Date().toDateString() ? hm : `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${hm}`;
}
function portRow(r) {
  const act = r.can_close
    ? `<button class="mini danger" data-port="${r.port}" data-pid="${r.pid}">✕ close</button>`
    : `<span class="dim small" title="${esc(r.kind_words)}: this card keeps it read-only">🔒 ${esc(r.kind_words)}</span>`;
  const nics = r.everyone ? ' <span class="pill warn" title="Listening on every interface, not only this PC">all nics</span>' : '';
  return `<div class="prow"><b>:${r.port}</b>`
    + `<span class="meta">${esc(r.name)}${r.label ? ` · ${esc(r.label)}` : ''}${nics}${chipTag(r.chip, r.kind === 'ai')}</span>`
    + `<span class="meta" title="${r.started ? `up since ${new Date(r.started * 1000).toLocaleString()}` : 'start time unknown'}">${upWords(r.up_seconds)}</span>`
    + `<span class="acts">${act}</span></div>`;
}
function closedRow(c) {
  return `<div class="prow"><b class="dim">:${c.port}</b>`
    + `<span class="meta">${esc(c.name)}${c.label ? ` · ${esc(c.label)}` : ''}</span>`
    + `<span class="meta">${c.ran_seconds == null ? '' : `ran ${upWords(c.ran_seconds)}`}</span>`
    + `<span class="acts meta">closed ${portWhen(c.closed_at)}${c.approx ? '*' : ''}</span></div>`;
}
async function loadPorts() {
  if (typeof cardShown === 'function' && !cardShown('c-ports')) return;   // hidden in this layout: not polled (layouts.js)
  let d;
  try { d = await (await fetch('/api/ports', { cache: 'no-store' })).json(); } catch (e) { return; }
  if (!d.ok) return;
  const all = d.listening || [], counts = d.counts || {}, closed = d.closed || [];
  const rows = all.filter((r) => portsAll || (r.kind !== 'system' && r.kind !== 'app'));
  const hidden = (counts.app || 0) + (counts.system || 0);
  $('portsSummary').textContent = `${all.length} listening · ${d.closable || 0} you can close`;
  setHTML($('portsList'), rows.length ? rows.map(portRow).join('') : '<div class="empty">nothing of yours is listening</div>');
  setHTML($('portsAll'), `${portsAll ? '–' : '+'} ${hidden} apps · Windows`);
  $('portsHistory').hidden = !closed.length;
  setHTML($('portsClosed'), closed.map(closedRow).join(''));
  $('portsApprox').hidden = !closed.some((c) => c.approx);
}
$('portsAll').onclick = () => {
  portsAll = !portsAll;
  $('portsAll').setAttribute('aria-pressed', String(portsAll));
  loadPorts();
};
$('portsRefresh').onclick = () => loadPorts();
$('portsList').addEventListener('click', async (e) => {
  const b = e.target.closest('button[data-port]');
  if (!b) return;
  const what = b.closest('.prow').querySelector('.meta').textContent.trim();
  if (!(await hudConfirm(`Close :${b.dataset.port}?\n\n${what}\n\nIt is asked to stop first, then forced if it ignores that. Anything unsaved in it is lost.`))) return;
  b.disabled = true;
  const r = await post('/api/action', { action: 'close_port', target: b.dataset.port, pid: Number(b.dataset.pid) });
  toast(r.why || (r.ok ? 'closed' : 'could not close it'));
  loadPorts();
});
setInterval(() => { if (!document.hidden) loadPorts(); }, 5000);

