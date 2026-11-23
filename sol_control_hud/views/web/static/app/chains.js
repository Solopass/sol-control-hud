// chain controls (split from app.js)
'use strict';
const OPS = { queued: ['now', 'cancel'], running: ['cancel'], waiting: ['now', 'cancel'], paused: ['run', 'resume'], cancel: [], };
const OP_LABEL = { run: 'Queue Run', now: '▶ Run now', swap_run: '⚡ Swap & Run', pause: 'Pause', resume: 'Resume', cancel: 'Cancel' };
let control = null;
async function loadControl() {
  if (document.hidden) return;
  if (typeof cardShown === 'function' && !['c-chains', 'c-ask', 'c-awayq'].some(cardShown)) return;   // hidden in this layout: not polled (layouts.js)
  try { control = await (await fetch('/api/control', { cache: 'no-store' })).json(); } catch (e) { return; }
  if (!$('askModel').options.length) askMode();
  const askState = (a) => a.state.startsWith('running') ? `<span style="color:var(--cyan)">${esc(a.state)}</span>`
    : a.state.startsWith('failed') ? `<span style="color:var(--red)" title="${esc(a.state)}">failed</span><button class="mini ghost" data-remove="${esc(a.job)}">clear</button>`
    : `waiting<button class="mini ghost" data-remove="${esc(a.job)}">remove</button>`;
  setHTML($('askList'), control.asks.length ? control.asks.map((a) => `<div class="item"><span>${esc(a.title)} <span class="dim">· ${esc(a.model)}</span></span>` +
    `<span>${askState(a)}</span></div>`).join('')
    : '<div class="empty">nothing waiting</div>');
  setHTML($('answerList'), control.answers.length ? control.answers.map((a) => `<div class="item link" data-answer="${esc(a.file)}">` +
    `<span><b>${esc(a.title)}</b> <span class="dim">· ${esc(when(a.time))}${a.model ? ' · ' + esc(a.model) : ''}</span></span><span class="pv">${esc(a.preview)}</span></div>`).join('')
    : '<div class="empty">no answers yet</div>');
  const chainSel = $('chainSelect');
  if (chainSel && control.chains) {
    const curVal = chainSel.value;
    chainSel.innerHTML = control.chains.map((c) => `<option value="${esc(c.name)}">${esc(c.name)} (${esc(c.status)})</option>`).join('');
    if (curVal && Array.from(chainSel.options).some((o) => o.value === curVal)) {
      chainSel.value = curVal;
    }
  }
  initChainLauncher();
  initFixQueueUI();
  loadFixQueue();
  if (schedEdit) return;                     // don't redraw under an open schedule editor
  renderChainList();
}

// ---------- the "All chains" list (B2, 10-19): one compact row per chain - its state, one line of what's next, the
// one action that fits its state, Result when there is one; Schedule, Inspect and the rest behind "⋯". Running and
// waiting chains first, then scheduled ones by next run, then the rest by name. The filter box narrows it.
let chainMore = null;                        // the row whose "⋯" is open
function chainRank(c) {
  if (c.status === 'running') return 0;
  if (c.active_waiting || c.blocked || c.status === 'waiting') return 1;
  if (c.reason || /need/i.test(c.status || '')) return 2;
  if (c.status === 'queued') return 3;
  if (c.status === 'paused') return 4;
  if (c.next_run) return 5;
  return 6;
}
function chainPrimary(c, ops) {
  if (c.active_waiting) return 'swap_run';
  if (c.blocked) return 'now';
  return ops[0] || null;
}
function renderChainList() {
  if (!control?.chains || schedEdit) return;  // don't redraw under an open schedule editor
  const q = ($('chainFilter')?.value || '').trim().toLowerCase();
  const rows = control.chains
    .filter((c) => !q || [c.name, c.status, c.model, c.schedule].some((x) => String(x || '').toLowerCase().includes(q)))
    .sort((a, b) => chainRank(a) - chainRank(b) || String(a.next_run || '~').localeCompare(String(b.next_run || '~'))
      || a.name.localeCompare(b.name));
  const opBtn = (c, o) => `<button class="mini${o === 'swap_run' ? ' swap-run' : o === 'now' ? ' go' : ''}" data-chain="${esc(c.name)}" data-op="${o}"`
    + `${o === 'cancel' ? ' data-confirm="Cancel this chain? It stops after its current step; finished steps are kept."' : ''}>${OP_LABEL[o] || o}</button>`;
  setHTML($('chainList'), rows.map((c) => {
    let ops = OPS[c.status] || ['run', 'pause'];
    if (c.status === 'running' && c.active_waiting) ops = ['swap_run', 'cancel'];
    const primary = chainPrimary(c, ops);
    const rest = ops.filter((o) => o !== primary);
    const meta = [];
    if (c.active_waiting) meta.push(`<span class="warnc" title="${esc(c.active_waiting)}">⏳ ${esc(c.active_waiting)}</span>`);
    else if (c.blocked) meta.push(`<span class="warnc" title="${esc(c.blocked)}">⏳ ${esc(c.blocked)}</span>`);
    else if (c.reason) meta.push(`<span class="badc" title="${esc(c.reason)}">⚠ ${esc(c.reason)}</span>`);
    else if (c.next_run) meta.push(`📅 ${esc(nextLabel(c.next_run))}`);
    else if (c.schedule) meta.push(`⏱ ${esc(c.schedule)}`);
    if (c.watch) meta.push('watches a folder');
    if (c.model) meta.push(esc(c.model));
    const plainMeta = meta.join(' · ').replace(/<[^>]+>/g, '');
    const statusClass = c.status === 'running' ? 'status running live-pulse' : `status ${esc(c.status)}`;
    const statusLabel = c.status === 'running' ? 'running ⚡' : esc(c.status);
    return `<div class="crow${chainMore === c.name ? ' more-open' : ''}" data-cname="${esc(c.name)}">`
      + `<span class="cname" title="${esc(c.name)}">${esc(c.name)}</span><span class="${statusClass}">${statusLabel}</span>`
      + `<span class="meta" title="${esc(plainMeta)}">${meta.join(' · ')}</span><span class="acts">`
      + (primary ? opBtn(c, primary) : '')
      + (c.result_time ? `<button class="mini ghost" data-result="${esc(c.name)}" title="result from ${esc(c.result_time)}">Result</button>` : '')
      + `<button class="mini ghost cmore-btn" data-cmore="${esc(c.name)}" title="Schedule, inspect and more" aria-expanded="${chainMore === c.name}">⋯</button>`
      + `<span class="cmore-items">`
      + `<button class="mini ${c.schedule ? 'on' : 'ghost'} sched-btn" data-sched="${esc(c.name)}" title="Change when it runs (current: ${esc(c.schedule || 'manual')})">⏱ ${esc(c.schedule || 'Schedule')}</button>`
      + `<button class="mini ${c.reason ? 'warn' : 'ghost'} inspect-btn" data-inspect="${esc(c.name)}" title="Inspect run events and log tail">🔍 Inspect</button>`
      + rest.map((o) => opBtn(c, o)).join('') + `</span></span></div>`
      + (inspectChain === c.name ? chainDetailsHtml(c) : '');
  }).join('') || (q ? '<div class="empty">no chain matches the filter</div>' : '<div class="empty">no chain notes in 1Notebook/Chains</div>'));
}
$('chainList')?.addEventListener('click', (e) => {
  const b = e.target.closest('[data-cmore]');
  if (!b) return;
  e.stopPropagation();
  chainMore = chainMore === b.dataset.cmore ? null : b.dataset.cmore;
  renderChainList();
});
$('chainFilter')?.addEventListener('input', () => renderChainList());

function initChainLauncher() {
  const sel = $('chainSelect');
  const btnRun = $('btnRunChainNow');
  const btnQueue = $('btnQueueChain');
  const btnSched = $('btnEditChainSchedule');
  const btnRes = $('btnViewChainResults');
  if (btnRun && !btnRun._init) {
    btnRun._init = true;
    btnRun.onclick = async () => {
      const name = sel ? sel.value : null;
      if (!name) return;
      btnRun.disabled = true;
      const r = await post('/api/action', { action: 'chain', target: name, op: 'swap_run' });
      toast(r ? r.why : `Queued ${name} (running now)`);
      btnRun.disabled = false;
      loadControl();
    };
  }
  if (btnQueue && !btnQueue._init) {
    btnQueue._init = true;
    btnQueue.onclick = async () => {
      const name = sel ? sel.value : null;
      if (!name) return;
      btnQueue.disabled = true;
      const r = await post('/api/action', { action: 'chain', target: name, op: 'run' });
      toast(r ? r.why : `Queued ${name}`);
      btnQueue.disabled = false;
      loadControl();
    };
  }
  if (btnSched && !btnSched._init) {
    btnSched._init = true;
    btnSched.onclick = () => {
      const name = sel ? sel.value : null;
      if (!name) return;
      openScheduleEditor(name);
    };
  }
  if (btnRes && !btnRes._init) {
    btnRes._init = true;
    btnRes.onclick = async () => {
      const name = sel ? sel.value : null;
      if (!name) return;
      btnRes.disabled = true;
      try {
        const a = await (await fetch(`/api/chain-result?name=${encodeURIComponent(name)}`)).json();
        if (a.ok) {
          openReader(`${name}: latest result`, a.text);
        } else {
          toast(a.why || 'No result found yet');
        }
      } catch (err) {
        toast('Could not fetch chain result');
      } finally {
        btnRes.disabled = false;
      }
    };
  }
}

