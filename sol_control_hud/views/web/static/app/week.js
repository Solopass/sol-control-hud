// chart range and this week (split from app.js)
'use strict';
// ---------- phase D: chart range + this week
document.querySelectorAll('#range button').forEach((b) => b.onclick = () => {
  range = b.dataset.range;
  document.querySelectorAll('#range button').forEach((x) => x.classList.toggle('on', x === b));
  loadHistory();
});
setInterval(() => { if (range !== '1h' && !document.hidden) loadHistory(); }, 60000);
async function loadWeek() {
  if (document.hidden) return;
  if (typeof cardShown === 'function' && !cardShown('c-week')) return;   // hidden in this layout: not polled (layouts.js)
  let w; try { w = await (await fetch('/api/week', { cache: 'no-store' })).json(); } catch (e) { return; }
  const chains = Object.entries(w.chains || {}).map(([k, n]) => `${n} ${k}`).join(', ') || 'none';
  const stat = (v, label, color) => `<div><b style="color:${color || 'var(--text)'}">${v ?? '–'}</b><span>${label}</span></div>`;
  setHTML($('weekStats'), [
    stat(w.away_hours, `Away hours (${w.away_runs} runs)`, 'var(--cyan)'),
    stat(w.jobs_done, 'jobs done', 'var(--green)'),
    stat(w.jobs_failed, 'jobs failed', w.jobs_failed ? 'var(--red)' : 'var(--dim)'),
    stat(w.crashes, 'crash events', w.crashes ? 'var(--red)' : 'var(--green)'),
    stat(w.hotspot_max != null ? `${Math.round(w.hotspot_max)}°` : null, 'hottest hotspot', w.hotspot_max >= 95 ? 'var(--red)' : 'var(--text)'),
    stat(w.vram_max != null ? `${w.vram_max} GB` : null, 'peak VRAM'),
    stat(w.spills ? `${w.spills_slow}/${w.spills}` : 0, 'spills that slowed answers', w.spills_slow ? 'var(--amber)' : 'var(--green)'),
  ].join(''));
  const disks = Object.entries(w.disks || {}).map(([d, g]) => `<span style="color:${g <= -5 ? 'var(--red)' : g >= 5 ? 'var(--green)' : 'var(--dim)'}">${esc(d)}: ${g > 0 ? '+' : ''}${g} GB</span>`).join(' · ');
  setHTML($('weekDisks'), `Chains: ${esc(chains)}${disks ? ' · Disks: ' + disks : ''}`);
  $('weekCover').textContent = w.covered_hours < 24 * w.days ? `from ${w.covered_hours} h of history so far` : `last ${w.days} days`;
}
setInterval(loadWeek, 300000);
// a tab opened in the background skipped its first load: fill the card as soon as it's looked at, not 5 min later
document.addEventListener('visibilitychange', () => { if (!document.hidden && !$('weekStats')?.children.length) loadWeek(); });

