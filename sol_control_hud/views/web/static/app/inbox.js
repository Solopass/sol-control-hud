// From your AI card (split from app.js)
'use strict';
// From your AI: the newest digest, open tasks, today's briefing focus, chains that need you, the bug hunt.
// The page sends ids ("digest", "tasks", ...) and a task's text + line; the server maps ids to files.
const INBOX_SEEN_KEY = 'sol_inbox_seen';
function inboxSeen() {
  try { return JSON.parse(localStorage.getItem(INBOX_SEEN_KEY) || '{}') || {}; } catch (e) { return {}; }
}
function inboxMarkSeen(sec, mtime) {
  try { const m = inboxSeen(); m[sec] = mtime; localStorage.setItem(INBOX_SEEN_KEY, JSON.stringify(m)); } catch (e) { /* private window */ }
}
function renderAiInbox(p) {
  if (typeof cardShown === 'function' && !cardShown('c-ai-inbox')) return;
  const d = p.ai_inbox || {};
  const box = $('inboxBody');
  const pill = $('inboxPill');
  if (!box || !pill) return;
  if (!d.available) { pill.textContent = '…'; pill.className = 'pill dim'; return; }
  const seen = inboxSeen();
  const fresh = (sec, o) => (o && o.mtime && o.mtime > (seen[sec] || 0));
  const dot = (sec, o) => (fresh(sec, o) ? ' <span class="inbox-new" title="changed since you last looked" style="color:var(--cyan)">●</span>' : '');
  const head = (sec, o, title, extra) => `<h3 class="inbox-h" data-inboxseen="${sec}" data-mtime="${o.mtime || 0}">${esc(title)}${dot(sec, o)}${extra || ''}</h3>`;
  const open = (id, label) => ` <button class="mini" data-inboxopen="${esc(id)}">${esc(label)}</button>`;
  const ul = (items) => `<ul class="inbox-list">${items.map((t) => `<li>${esc(t)}</li>`).join('')}</ul>`;
  let html = '', news = 0;
  if (d.needs && d.needs.chains.length) {
    html += `<div class="inbox-sec">${head('needs', d.needs, 'Needs you')}`
      + d.needs.chains.map((c) => `<div class="row"><span><span style="color:var(--amber)">●</span> ${esc(c.name)} <span class="dim">· ${esc(c.status)}${c.reason ? ` · ${esc(c.reason)}` : ''}</span></span>${open('chain:' + c.name, 'Open')}</div>`).join('')
      + '</div>';
    if (fresh('needs', d.needs)) news++;
  }
  if (d.work_summary) {
    const w = d.work_summary;
    html += `<div class="inbox-sec">${head('work_summary', w, `Daily work · ${w.date || w.name}`, open('work_summary', 'Open summary'))}`
      + (w.completed.length ? `<div class="dim">Completed</div>${ul(w.completed)}` : '')
      + (w.open.length ? `<div class="dim">Still open</div>${ul(w.open)}` : '') + '</div>';
    if (fresh('work_summary', w)) news++;
  }
  if (d.digest) {
    const g = d.digest;
    html += `<div class="inbox-sec">${head('digest', g, `This week · ${g.date || g.name}`, open('digest', 'Open digest'))}`
      + (g.next.length ? `<div class="dim">Next week</div>${ul(g.next)}` : '')
      + (g.loops.length ? `<div class="dim">Open loops</div>${ul(g.loops)}` : '') + '</div>';
    if (fresh('digest', g)) news++;
  }
  if (d.briefing) {
    const b = d.briefing;
    html += `<div class="inbox-sec">${head('briefing', b, "Today's focus", open('briefing', 'Open briefing'))}`
      + (b.focus ? `<div>${esc(b.focus)}</div>` : '') + (b.attention.length ? ul(b.attention) : '') + '</div>';
    if (fresh('briefing', b)) news++;
  }
  if (d.tasks) {
    const t = d.tasks;
    html += `<div class="inbox-sec">${head('tasks', t, 'Open tasks', open('tasks', 'Open Tasks.md'))}`
      + (t.open.length ? t.open.map((k) => `<label class="row"><span><input type="checkbox" class="switch" data-inboxtick="${k.line}" data-text="${esc(k.text)}"> ${esc(k.text)}</span></label>`).join('')
        : '<div class="empty">no open tasks</div>')
      + (t.more ? `<div class="dim">+${t.more} more</div>` : '') + '</div>';
    if (fresh('tasks', t)) news++;
  }
  if (d.bughunt) {
    const h = d.bughunt;
    html += `<div class="inbox-sec">${head('bughunt', h, 'Bug hunt', open('bughunt', 'Open result'))}${ul(h.items)}${h.more ? `<div class="dim">+${h.more} more</div>` : ''}</div>`;
    if (fresh('bughunt', h)) news++;
  }
  pill.textContent = news ? `${news} NEW` : 'OK';
  pill.className = `pill ${news ? 'ok' : 'dim'}`;
  setHTML(box, html || '<div class="empty">nothing from your AI yet</div>');
}
$('inboxBody')?.addEventListener('click', async (e) => {
  const o = e.target.closest('[data-inboxopen]');
  if (o) {
    const sec = o.closest('.inbox-sec')?.querySelector('[data-inboxseen]');
    if (sec) { inboxMarkSeen(sec.dataset.inboxseen, Number(sec.dataset.mtime)); }
    const r = await post('/api/action', { action: 'ai_inbox_open', target: o.dataset.inboxopen });
    if (r && r.ok === false) toast(r.why || 'could not open');
    return;
  }
  const h = e.target.closest('[data-inboxseen]');
  if (h) { inboxMarkSeen(h.dataset.inboxseen, Number(h.dataset.mtime)); h.querySelector('.inbox-new')?.remove(); }
});
$('inboxBody')?.addEventListener('change', async (e) => {
  const c = e.target.closest('[data-inboxtick]');
  if (!c) return;
  c.disabled = true;
  const r = await post('/api/action', { action: 'ai_inbox_tick', text: c.dataset.text, line: Number(c.dataset.inboxtick) });
  if (!r || r.ok === false) { toast((r && r.why) || 'could not tick it'); c.checked = false; c.disabled = false; }
  else toast('Ticked off');
});

