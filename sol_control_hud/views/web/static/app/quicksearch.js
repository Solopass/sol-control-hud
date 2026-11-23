// quickbar search (split from app.js)
'use strict';
// ---------- Quick Search in the Quickbar (#qbFilter): never hides cards. Dropdown = Cards (instant, from the active
// layout via layouts.js quickCardMatches) + Files & notes (/api/search). >= 2 chars for both.
function initQuickSearch() {
  const input = $('qbFilter');
  const box = $('qbSearchResults');
  if (!input || !box) return;

  let debounce = null;
  let seq = 0;          // bumped on every query change/close: a fetch answering for an older seq is ignored
  let cards = [];
  let hits = null;      // null = not answered yet (or failed)
  let failed = false;
  let sel = -1;

  const items = () => [...box.querySelectorAll('.qb-search-item')];
  const close = () => { seq++; clearTimeout(debounce); box.hidden = true; sel = -1; };

  function highlight(n) {
    const els = items();
    if (!els.length) { sel = -1; return; }
    sel = (n + els.length) % els.length;
    els.forEach((el, i) => el.classList.toggle('sel', i === sel));
    els[sel].scrollIntoView({ block: 'nearest' });
  }

  function render(pending) {
    const parts = [];
    if (cards.length) {
      parts.push('<div class="qb-search-head">Cards</div>' + cards.map((c) =>
        `<div class="qb-search-item" data-card="${esc(c.id)}"><div class="qb-search-item-path">▦ ${esc(c.title)}` +
        `${c.hidden ? ' <span class="dim">(hidden)</span>' : ''}</div></div>`).join(''));
    }
    if (hits && hits.length) {
      parts.push('<div class="qb-search-head">Files &amp; notes</div>' + hits.map((h) => {
        const fname = h.path.split(/[\\/]/).pop();
        return `<div class="qb-search-item" data-path="${esc(h.path)}" data-line="${esc(h.start)}">` +
          `<div class="qb-search-item-path">📄 ${esc(fname)} <span class="dim">:${esc(h.start)}-${esc(h.end)}</span></div>` +
          `<div class="qb-search-item-snippet">${esc(h.snippet)}</div>` +
          `<div class="qb-search-meta"><span class="dim">${esc(h.path)}</span><span>${h.score ? Math.round(h.score * 100) / 100 : ''}</span></div>` +
          `</div>`;
      }).join(''));
    } else if (pending) {
      parts.push('<div class="qb-search-empty dim">Searching files &amp; notes…</div>');
    } else if (!cards.length) {
      parts.push(`<div class="qb-search-empty dim">${failed ? 'Search unavailable' : 'No matches'}</div>`);
    }
    box.innerHTML = parts.join('');
    box.hidden = false;
    sel = -1;
    highlight(0);
  }

  function refresh() {
    const q = input.value.trim();
    clearTimeout(debounce);
    const mine = ++seq;
    if (q.length < 2) { box.hidden = true; sel = -1; return; }
    cards = typeof window.quickCardMatches === 'function' ? window.quickCardMatches(q, 5) : [];
    hits = null;
    failed = false;
    render(true);
    debounce = setTimeout(async () => {
      try {
        const d = await (await fetch(`/api/search?q=${encodeURIComponent(q)}&k=6`)).json();
        if (mine !== seq) return;
        hits = d && d.ok && Array.isArray(d.hits) ? d.hits : [];
        failed = !(d && d.ok);
      } catch (e) {
        if (mine !== seq) return;
        hits = [];
        failed = true;
      }
      render(false);
    }, 250);
  }

  async function activate(item) {
    if (!item) return;
    input.value = '';
    close();
    if (item.dataset.card) {
      if (typeof window.jumpToCard === 'function') window.jumpToCard(item.dataset.card);
      return;
    }
    const p = item.dataset.path;
    if (!p) return;
    if (p.endsWith('.md')) {
      const parts = p.split(/[\\/]/);
      const idx = parts.findIndex(x => x.toLowerCase().includes('vault') || x === 'OBVLT');
      const vault = idx !== -1 ? parts[idx] : 'OBVLT';
      const rel = idx !== -1 ? parts.slice(idx + 1).join('/') : p;
      await post('/api/open-note', { vault, file: rel });
      toast(`Opened in Obsidian: ${parts.pop()}`);
    } else {
      toast(`Path: ${p}`);
    }
  }

  input.addEventListener('input', refresh);
  input.addEventListener('focus', () => { if (input.value.trim().length >= 2 && box.hidden) refresh(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      input.value = '';
      close();
      input.blur();
    } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (box.hidden) return;
      e.preventDefault();
      highlight(sel + (e.key === 'ArrowDown' ? 1 : -1));
    } else if (e.key === 'Enter') {
      if (box.hidden) return;
      e.preventDefault();
      const els = items();
      activate(els[sel] || els[0]);
    }
  });

  box.addEventListener('click', (e) => activate(e.target.closest('.qb-search-item')));

  document.addEventListener('click', (e) => {
    if (!e.target.closest('.qb-search-wrap')) box.hidden = true;
  });
}

