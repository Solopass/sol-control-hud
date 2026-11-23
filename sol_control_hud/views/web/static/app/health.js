// the HUD's own health (split from app.js)
'use strict';
// ---------- E1: the HUD's own health (health.py via /api/health)
async function loadHealth() {
  if (document.hidden) return;
  if (typeof cardShown === 'function' && !cardShown('c-health')) return;   // hidden in this layout: not polled (layouts.js)
  let h; try { h = await (await fetch('/api/health', { cache: 'no-store' })).json(); } catch (e) { return; }
  if (!h.ok) return;
  const pill = $('healthPill');
  pill.textContent = { ok: 'OK', warn: 'LOOK', bad: 'TROUBLE' }[h.level] || '…';
  pill.className = `pill ${h.level === 'bad' ? 'bad' : h.level === 'warn' ? 'warn' : 'ok'}`;
  $('healthUp').textContent = h.up_since ? `${h.up_since.slice(0, 10) === new Date().toISOString().slice(0, 10) ? 'today' : h.up_since.slice(5, 10)} ${hhmm(h.up_since)} · pid ${h.pid}` : `pid ${h.pid}`;
  const r = h.restarts || {}, rc = $('healthRestarts');
  rc.textContent = `${r.week || 0} this week${r.day ? ` · ${r.day} today` : ''}`;
  rc.style.color = r.day >= 3 ? 'var(--red)' : r.week ? 'var(--amber)' : 'var(--green)';
  const c = h.cost || {};
  $('healthCost').innerHTML = c.cpu == null ? '–' : `<span style="color:${c.cpu > c.target_cpu * 2 ? 'var(--amber)' : 'var(--text)'}">${c.cpu.toFixed(1)} % CPU</span> · ${Math.round(c.ram_mb || 0)} MB`;
  const notes = (h.stale || []).map((s) => s.why === 'stopped' ? `${s.name} stopped` : `${s.name}: no update for ${s.age_s} s`);
  if ((h.broken || []).length) notes.push(`failing: ${h.broken.join(', ')}`);
  $('healthStale').textContent = notes.join(' · ');
  $('healthStale').style.color = notes.length ? 'var(--amber)' : '';
  const when = (t) => { if (!t) return ''; const d = new Date(t * 1000); return `${d.toDateString() === new Date().toDateString() ? '' : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ' '}${d.toTimeString().slice(0, 5)}`; };
  setHTML($('healthErrors'), (h.errors || []).length
    ? h.errors.map((e) => `<details><summary><span class="dim">${esc(when(e.at))} ${esc(e.source)}</span> ${esc(e.text)}</summary><pre>${esc(e.detail)}</pre></details>`).join('')
    : '<div class="note">no errors in its logs</div>');
  const q = h.handled || {};                   // swallow.note: errors it carried on after (one line per place an hour)
  setHTML($('healthHandled'), q.day
    ? `<details><summary><span class="dim">handled quietly, last 24 h:</span> ${q.day} at ${q.places} place${q.places === 1 ? '' : 's'}</summary>`
      + `<pre>${esc((q.recent || []).map((e) => `${when(e.at)} ${e.where}: ${e.text}`).join('\n'))}</pre></details>`
    : '');
}
loadHealth();
setInterval(loadHealth, 15000);

