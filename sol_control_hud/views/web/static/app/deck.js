// Deck services card (split from app.js)
'use strict';
// Deck services: the autostart projects (sol-deck, sol-cs2), up or down, the last autostart result, the iPad client.
function renderDeckServices(p) {
  if (typeof cardShown === 'function' && !cardShown('c-deck')) return;
  const d = p.deck_services || {};
  const box = $('deckRows');
  const pill = $('deckPill');
  if (!box || !pill) return;
  const svcs = d.services || [];
  if (!d.available) { pill.textContent = '…'; pill.className = 'pill dim'; return; }
  const down = svcs.filter((s) => !s.up);
  pill.textContent = down.length ? `${down.length} DOWN` : 'ALL UP';
  pill.className = `pill ${down.length ? 'red' : 'ok'}`;
  setHTML(box, svcs.map((s) => {
    const a = s.autostart;
    const dot = `<span style="color:${s.up ? 'var(--green)' : 'var(--red)'}">●</span>`;
    const state = s.up
      ? `<span style="color:var(--green)">up</span> <span class="dim">${s.up_seconds ? `· ${upWords(s.up_seconds)}` : ''}${s.pid ? ` · pid ${s.pid}` : ''}</span>`
      : `<span style="color:var(--red)">down</span>${s.busy_by ? ` <span class="dim">· :${s.port} taken by ${esc(s.busy_by)}</span>` : ''}`;
    const btn = s.can_start ? `<button class="mini go" data-deckstart="${esc(s.name)}" title="Start ${esc(s.title)} (same as the Projects card)">▶ Start</button>` : '';
    const auto = a
      ? `<div class="note" style="${a.ok ? '' : 'color:var(--amber)'}">autostart ${a.ok ? 'ok' : 'failed'}: ${esc(a.why)} <span class="dim">· ${upWords(a.ago_s)} ago</span></div>`
      : '';
    return `<div class="row"><span>${dot} ${esc(s.title)}${s.port ? ` <span class="dim">:${s.port}</span>` : ''}</span><span>${state} ${btn}</span></div>${auto}`;
  }).join('') || '<div class="empty">no autostart projects</div>');
  const ip = d.ipad || {};
  const ipEl = $('deckIpad');
  if (ipEl) {
    ipEl.innerHTML = !ip.deck_up ? '📱 iPad deck: <span class="dim">sol-deck is down, so no client can connect</span>'
      : ip.active ? `📱 iPad deck: <span style="color:var(--green)">${esc(ip.status_text || 'connected')}</span>`
      : `📱 iPad deck: <span style="color:var(--amber)">not seen recently</span> <span class="dim">· ${esc(ip.status_text || '')}</span>`;
  }
}
$('deckRows')?.addEventListener('click', (e) => {
  const b = e.target.closest('[data-deckstart]');
  if (b && typeof launch === 'function') { b.disabled = true; launch('project', b.dataset.deckstart, 'start'); }
});

