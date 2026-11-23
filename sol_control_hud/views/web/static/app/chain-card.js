// chain schedules and the chain card (split from app.js)
'use strict';
// ---------- chain schedules: edit `schedule:` from the card (the server checks it with the runner's own parser)
let schedEdit = null;
const DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
function schedParts(text) {
  const t = String(text || '').trim().toLowerCase();
  if (!t) return { kind: '', time: '07:00', days: [] };
  if (t === 'on away') return { kind: 'away', time: '07:00', days: [] };
  let m = t.match(/^daily (\d{1,2}):(\d{2})$/);
  if (m) return { kind: 'daily', time: `${m[1].padStart(2, '0')}:${m[2]}`, days: [] };
  m = t.match(/^weekdays (\d{1,2}):(\d{2})$/);
  if (m) return { kind: 'weekdays', time: `${m[1].padStart(2, '0')}:${m[2]}`, days: ['mon', 'tue', 'wed', 'thu', 'fri'] };
  m = t.match(/^weekends (\d{1,2}):(\d{2})$/);
  if (m) return { kind: 'weekends', time: `${m[1].padStart(2, '0')}:${m[2]}`, days: ['sat', 'sun'] };
  m = t.match(/^weekly ([a-z, ]+?) (\d{1,2}):(\d{2})$/);
  if (m) {
    const days = m[1].split(/[ ,]+/).map((d) => d.slice(0, 3));
    const lowerDays = days.map((d) => d.toLowerCase());
    if (lowerDays.length === 5 && ['mon', 'tue', 'wed', 'thu', 'fri'].every((d) => lowerDays.includes(d))) {
      return { kind: 'weekdays', time: `${m[2].padStart(2, '0')}:${m[3]}`, days: lowerDays };
    }
    if (lowerDays.length === 2 && ['sat', 'sun'].every((d) => lowerDays.includes(d))) {
      return { kind: 'weekends', time: `${m[2].padStart(2, '0')}:${m[3]}`, days: lowerDays };
    }
    return { kind: 'weekly', time: `${m[2].padStart(2, '0')}:${m[3]}`, days: lowerDays };
  }
  return { kind: 'custom', time: '07:00', days: [], raw: text };
}
function schedEditor(c) {
  const s = schedParts(c.schedule);
  const days = DAY_NAMES.map((d) => `<label><input type="checkbox" value="${d}"${s.days.includes(d.toLowerCase()) ? ' checked' : ''}>${d}</label>`).join('');
  const isPaused = c.status === 'paused';
  return `<div class="sched-edit" data-for="${esc(c.name)}"><b>${esc(c.name)}</b> runs ` +
    `<select class="sk">` +
    `<option value=""${s.kind === '' ? ' selected' : ''}>never by itself (manual)</option>` +
    `<option value="away"${s.kind === 'away' ? ' selected' : ''}>when Away starts (on away)</option>` +
    `<option value="daily"${s.kind === 'daily' ? ' selected' : ''}>every day</option>` +
    `<option value="weekdays"${s.kind === 'weekdays' ? ' selected' : ''}>weekdays (Mon–Fri)</option>` +
    `<option value="weekends"${s.kind === 'weekends' ? ' selected' : ''}>weekends (Sat–Sun)</option>` +
    `<option value="weekly"${s.kind === 'weekly' ? ' selected' : ''}>on specific days</option>` +
    `<option value="custom"${s.kind === 'custom' ? ' selected' : ''}>custom text</option>` +
    `</select>` +
    `<span class="sdays"${s.kind === 'weekly' ? '' : ' hidden'}>${days}</span>` +
    `<span class="stime"${s.kind === 'daily' || s.kind === 'weekly' || s.kind === 'weekdays' || s.kind === 'weekends' ? '' : ' hidden'}>at <input type="time" class="st" value="${s.time}"></span>` +
    `<span class="scustom-wrap"${s.kind === 'custom' ? '' : ' hidden'}><input type="text" class="scustom" placeholder="e.g. daily 08:30 or on away" value="${esc(s.raw || c.schedule || '')}" style="width:160px;padding:3px 6px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--text);font-size:12px;"></span>` +
    `<button class="mini go" data-sched-save>Save</button>` +
    (isPaused ? ' <button class="mini ok" data-sched-save-active title="Save schedule and unpause this chain">Save &amp; Activate</button>' : '') +
    (c.schedule ? ' <button class="mini danger ghost" data-sched-clear title="Remove schedule completely">Remove</button>' : '') +
    ' <button class="mini ghost" data-sched-cancel>Cancel</button>' +
    (isPaused ? '<span class="dim small" style="margin-left:6px">⚠️ paused: Click "Save &amp; Activate" or Resume it for the schedule to take effect</span>' : '') + '</div>';
}

let inspectChain = null;
const chainDetailsCache = {};

function chainDetailsHtml(c) {
  const d = chainDetailsCache[c.name];
  if (!d) {
    return `<div class="chain-details-box"><div class="dim small">Loading execution logs & events for ${esc(c.name)}…</div></div>`;
  }
  const run = d.run;
  const isErr = Boolean(d.reason || (run && run.status !== 'succeeded'));
  let runSummary = '';
  if (run) {
    const dur = (run.started_at && run.finished_at) ? ` · ${(run.finished_at - run.started_at).toFixed(1)}s` : '';
    runSummary = `<span class="dim small">Run #${run.id} (${run.status}${dur})</span>`;
  }
  let errMsg = '';
  if (d.reason || run?.error) {
    errMsg = `<div class="chain-err-msg">⚠️ <b>Error:</b> ${esc(run?.error || d.reason)}</div>`;
  }

  let eventsHtml = '';
  if (run && Array.isArray(run.events) && run.events.length) {
    eventsHtml = `<div class="chain-events-list">` + run.events.map((ev) => {
      const cls = ev.kind || '';
      const stepTxt = ev.step ? `<b>[${esc(ev.step)}]</b> ` : '';
      const evData = ev.data ? Object.entries(ev.data).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(' ') : '';
      return `<div class="chain-event-row ${cls}">`
        + `<span class="dim" style="font-size:10px;">${new Date(ev.at * 1000).toLocaleTimeString()}</span>`
        + `<span>${stepTxt}${esc(ev.kind)}</span>`
        + `<span class="dim small">${esc(evData)}</span></div>`;
    }).join('') + `</div>`;
  }

  let logHtml = '';
  if (Array.isArray(d.log_tail) && d.log_tail.length) {
    logHtml = `<pre class="chain-log-tail">${esc(d.log_tail.join('\n'))}</pre>`;
  }

  return `<div class="chain-details-box ${isErr ? '' : 'is-ok'}" data-for="${esc(c.name)}">`
    + `<div class="chain-details-header">`
    + `<b>🔍 ${esc(c.name)} Execution Details</b>${runSummary}`
    + `<div style="display:flex;gap:4px;align-items:center;">`
    + `<button class="mini go" data-chain="${esc(c.name)}" data-op="now" title="Run now">▶ Run now</button>`
    + `<button class="mini swap-run" data-chain="${esc(c.name)}" data-op="swap_run" title="Swap & Run">⚡ Swap & Run</button>`
    + `<button class="mini ghost" data-close-inspect="${esc(c.name)}" title="Close details">✕ Close</button>`
    + `</div></div>`
    + errMsg
    + (eventsHtml ? `<div class="dim small" style="margin-top:2px;"><b>Recent Events:</b></div>${eventsHtml}` : '')
    + (logHtml ? `<div class="dim small" style="margin-top:2px;"><b>Engine Log Tail:</b></div>${logHtml}` : '')
    + `</div>`;
}

function openScheduleEditor(chainName) {
  inspectChain = null;
  const c = (control?.chains || []).find((x) => x.name === chainName);
  document.querySelectorAll('.sched-edit').forEach((el) => el.remove());
  if (!c || schedEdit === c.name) { schedEdit = null; return; }
  schedEdit = c.name;
  const row = document.querySelector(`.crow [data-sched="${CSS.escape(c.name)}"]`)?.closest('.crow');
  if (row) {
    row.insertAdjacentHTML('afterend', schedEditor(c));
    row.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } else {
    const list = $('chainList');
    if (list) {
      list.insertAdjacentHTML('afterbegin', schedEditor(c));
      list.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }
}

const cChains = $('c-chains');
if (cChains && !cChains._schedInit) {
  cChains._schedInit = true;
  cChains.addEventListener('click', async (e) => {
    const ins = e.target.closest('[data-inspect]');
    if (ins) {
      e.stopPropagation();
      const name = ins.dataset.inspect;
      if (inspectChain === name) {
        inspectChain = null;
        loadControl();
        return;
      }
      inspectChain = name;
      schedEdit = null;
      loadControl();
      try {
        const d = await (await fetch(`/api/chain-details?name=${encodeURIComponent(name)}`)).json();
        if (d.ok) {
          chainDetailsCache[name] = d;
          loadControl();
        }
      } catch (_) {}
      return;
    }
    const closeIns = e.target.closest('[data-close-inspect]');
    if (closeIns) {
      e.stopPropagation();
      inspectChain = null;
      loadControl();
      return;
    }
    const open = e.target.closest('[data-sched]');
    if (open) {
      e.stopPropagation();
      openScheduleEditor(open.dataset.sched);
      return;
    }
    const box = e.target.closest('.sched-edit');
    if (!box) return;
    if (e.target.closest('[data-sched-cancel]')) { e.stopPropagation(); box.remove(); schedEdit = null; return; }
    if (e.target.closest('[data-sched-clear]')) {
      e.stopPropagation();
      const r = await post('/api/action', { action: 'chain', target: box.dataset.for, op: 'schedule', schedule: '' });
      toast(r.why || (r.ok ? 'Schedule removed' : 'that did not work'));
      if (r.ok) { box.remove(); schedEdit = null; loadControl(); }
      return;
    }
    const isSave = Boolean(e.target.closest('[data-sched-save]'));
    const isSaveActive = Boolean(e.target.closest('[data-sched-save-active]'));
    if (!isSave && !isSaveActive) return;
    e.stopPropagation();
    const kind = box.querySelector('.sk').value, time = box.querySelector('.st').value || '07:00';
    const days = [...box.querySelectorAll('.sdays input:checked')].map((i) => i.value);
    if (kind === 'weekly' && !days.length) { toast('pick at least one day'); return; }
    let schedule = '';
    if (kind === 'daily') schedule = `daily ${time}`;
    else if (kind === 'weekdays') schedule = `weekdays ${time}`;
    else if (kind === 'weekends') schedule = `weekends ${time}`;
    else if (kind === 'weekly') schedule = `weekly ${days.join(',')} ${time}`;
    else if (kind === 'away') schedule = 'on away';
    else if (kind === 'custom') schedule = (box.querySelector('.scustom')?.value || '').trim();
    const r = await post('/api/action', { action: 'chain', target: box.dataset.for, op: 'schedule', schedule });
    if (isSaveActive && r.ok) {
      await post('/api/action', { action: 'chain', target: box.dataset.for, op: 'resume' });
      toast(`Saved & activated ${box.dataset.for}`);
    } else {
      toast(r.why || (r.ok ? 'saved' : 'that did not work'));
    }
    if (r.ok) { box.remove(); schedEdit = null; loadControl(); }
  }, true);
  cChains.addEventListener('change', (e) => {
    const box = e.target.closest('.sched-edit');
    if (!box || !e.target.classList.contains('sk')) return;
    const k = e.target.value;
    box.querySelector('.sdays').hidden = k !== 'weekly';
    box.querySelector('.stime').hidden = !(k === 'daily' || k === 'weekly' || k === 'weekdays' || k === 'weekends');
    const cust = box.querySelector('.scustom-wrap');
    if (cust) cust.hidden = k !== 'custom';
  });
}
const pendingModels = new Map();   // model -> the state it had when you pressed its button (shown as "…" until it changes)
document.addEventListener('click', async (e) => {
  const t = e.target.closest('[data-model],[data-models],[data-power]');
  if (!t || t.disabled) return;
  e.stopPropagation();
  if (t.dataset.confirm && !(await hudConfirm(t.dataset.confirm))) return;
  let r;
  if (t.dataset.model) {
    const cur = (payload?.models || []).find((m) => m.id === t.dataset.model);
    if (cur) pendingModels.set(cur.id, cur.state);
    r = await post('/api/action', { action: 'model', target: t.dataset.model, op: t.dataset.op });
  } else if (t.dataset.models) r = await post('/api/action', { action: 'models_unload_all' });
  else {
    if (t.dataset.power === 'off' && !(await hudConfirm('Turn the local AI off? Every model unloads and nothing loads until you turn it on again (chains and asks wait).'))) return;
    r = await post('/api/action', { action: 'ai_power', target: t.dataset.power });
  }
  if (r?.why) toast(r.why);
  if (r && !r.ok && t.dataset.model) pendingModels.delete(t.dataset.model);
}, true);

document.addEventListener('click', async (e) => {
  const t = e.target.closest('[data-chain],[data-remove],[data-answer],[data-result]');
  if (!t || t.disabled) return;
  e.stopPropagation();
  if (t.dataset.confirm && !(await hudConfirm(t.dataset.confirm))) return;
  t.disabled = true;
  try {
    let r = null;
    if (t.dataset.chain) r = await post('/api/action', { action: 'chain', target: t.dataset.chain, op: t.dataset.op });
    else if (t.dataset.remove) r = await post('/api/action', { action: 'ask_remove', target: t.dataset.remove });
    else if (t.dataset.answer) { const a = await (await fetch(`/api/answer?file=${encodeURIComponent(t.dataset.answer)}`)).json(); if (a.ok) openReader(t.dataset.answer.replace(/\.md$/, ''), a.text); else toast(a.why); return; }
    else if (t.dataset.result) { const a = await (await fetch(`/api/chain-result?name=${encodeURIComponent(t.dataset.result)}`)).json(); if (a.ok) openReader(`${t.dataset.result}: latest result`, a.text); else toast(a.why); return; }
    if (r?.why) toast(r.why);
    loadControl();
  } finally {
    t.disabled = false;
  }
}, true);
// "Run now" swaps the Away models (tonight) for the Desk ones (right away); the choice is remembered per browser.
function askMode() {
  if (!control) return;
  const now = $('askNow').checked, list = now ? (control.desk_models || []) : control.models;
  try { localStorage.setItem('askNow', now ? '1' : ''); } catch (e) { /* private window */ }
  $('askModel').innerHTML = list.map((m) => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join('');
  if (now) $('askModel').value = 'sol-smart';
  $('askGo').textContent = now ? 'Ask now' : 'Queue for tonight';
  $('askHead').textContent = now ? 'Ask it now' : 'Ask it overnight';
  $('askSub').textContent = now ? 'a Desk model answers right away (takes its turn on the GPU) · answer in 1Notebook\\Answers'
    : 'runs in the next Away session · answer in 1Notebook\\Answers';
}
try { $('askNow').checked = localStorage.getItem('askNow') === '1'; } catch (e) { /* private window */ }
$('askNow').onchange = askMode;
$('askForm').onsubmit = async (e) => {
  e.preventDefault();
  const btn = e.target.querySelector('button[type=submit]'); btn.disabled = true;
  const files = $('askFiles').value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
  const r = await post('/api/action', { action: 'ask', title: $('askTitle').value, question: $('askQuestion').value, files,
    model: $('askModel').value, now: $('askNow').checked });
  btn.disabled = false; toast(r.why || (r.ok ? 'queued' : 'failed'));
  if (r.ok) { $('askTitle').value = ''; $('askQuestion').value = ''; $('askFiles').value = ''; }
  loadControl();
};
setInterval(loadControl, 15000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) loadControl(); });
