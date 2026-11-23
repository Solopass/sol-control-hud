// game guard (split from app.js)
'use strict';
// ---------- the game guard: why the AI is off, and the two buttons that answer it
// It is right about games and wrong about plenty else (Notepad, Edge at 54%, claude x3, steamwebhelper x5). Each
// of those was fixed by hand-editing local-ai.json while this page displayed the reason and offered nothing.
// Only "<name> uses N% of the GPU" can be answered by name; the server decides that, not this.
async function loadGuard() {
  if (typeof cardShown === 'function' && !cardShown('c-ai')) return;   // hidden in this layout: not polled (layouts.js)
  let g;
  try { g = await (await fetch('/api/guard', { cache: 'no-store' })).json(); } catch (e) { return; }
  const box = $('guardBox');
  if (!box) return;
  if (!g.ok || !g.available || !g.off_for_a_game) { box.hidden = true; return; }
  const acts = [];
  if (g.can_ignore) acts.push(`<button class="mini" data-act="guard_ignore" data-target="${esc(g.name)}">Not a game — ignore ${esc(g.name)}</button>`);
  acts.push('<button class="mini ghost" data-act="guard_resume" data-confirm="Go back to desk now? Do this once whatever tripped the guard is gone, or it will just turn off again.">Resume now</button>');
  setHTML(box, `<div class="guard-why">⏸ AI off — <b>${esc(g.detail || g.reason || 'a game')}</b></div>`
             + `<div class="guard-acts">${acts.join('')}</div>`
             + (g.can_ignore ? '' : '<div class="dim small">That looks like a real game, so there is nothing to ignore.</div>'));
  box.hidden = false;
}
setInterval(() => { if (!document.hidden) loadGuard(); }, 5000);

