// ▦ Layouts: presets of which cards show, in what order, at what size (layouts.py, GET/POST /api/layouts).
// Loaded after app.js / launchpad.js / settings.js and uses their helpers ($, esc, post, toast, drawCharts).
// The cards' own renderers are untouched: a size only changes the card's grid span and which of its parts show
// (app.css `.card[data-size=..]`), and an XS card shows one "chip" line filled here from the live data.
'use strict';

let LAYOUT = null;          // the server's state: {active, auto, rev, presets, widgets, slides}
let EDIT = null;            // while editing: {old: name, preset: working copy}
const LGRID = document.querySelector('main.grid');
const lcard = (id) => document.getElementById(id);
const LC = window.LayoutCore;    // the pure parts (layouts-core.js), tested with node

// which loader feeds which card: a hidden card isn't polled, and is loaded at once when it comes back
const LOADERS = { 'c-ports': 'loadPorts', 'c-notes': 'loadNotes', 'c-ai': 'loadGuard', 'c-gpu': 'loadDisplays', 'c-week': 'loadWeek',
  'c-ai-usage': 'loadAiUsage', 'c-procs': 'loadProcs', 'c-media-ctl': 'loadMediaSession',
  'c-health': 'loadHealth', 'c-projects': 'loadProjects', 'c-media': 'loadMedia', 'c-chains': 'loadControl', 'c-ask': 'loadControl',
  'c-awayq': 'loadControl', 'c-overnight': 'loadOvernight' };

function cardShown(id) { const el = lcard(id); return !el || !el.hidden; }      // app.js / launchpad.js ask this

function gridCols() {
  const t = getComputedStyle(LGRID).gridTemplateColumns || '';
  return Math.max(1, t.split(' ').filter(Boolean).length);
}
function reg(id) { return (LAYOUT?.widgets || []).find((w) => w.id === id); }
function activePreset() { return (LAYOUT?.presets || []).find((p) => p.name === LAYOUT.active) || LAYOUT?.presets?.[0]; }

// A hidden tab can't animate (the browser aborts the transition), so it just applies the change. A running transition
// replaced by a new one, or aborted by a timeout, rejects its promises: those aborts are expected and ignored. If `fn`
// itself throws, updateCallbackDone rejects with that error: it is logged, not hidden (ready/finished carry the same
// error, so they stay silent to avoid logging it three times).
const VT_ABORTS = ['AbortError', 'InvalidStateError', 'TimeoutError'];
function withViewTransition(fn) {
  const canAnimate = typeof document.startViewTransition === 'function' && document.visibilityState === 'visible'
    && !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!canAnimate) { fn(); return; }
  const vt = document.startViewTransition(fn);
  const quiet = () => {};
  vt.ready.catch(quiet);
  vt.finished.catch(quiet);
  vt.updateCallbackDone.catch((err) => {
    if (!VT_ABORTS.includes(err?.name)) console.error('view transition callback failed:', err);
  });
}

// ---------- applying a preset to the page
function applyPreset(p) {
  if (!p) return;
  withViewTransition(() => {
    const cols = gridCols(), comeBack = [];
    for (const w of p.widgets) {
      const el = lcard(w.id); const r = reg(w.id);
      if (!el || !r) continue;
      LGRID.appendChild(el);                                   // DOM order = preset order
      const hide = !!w.hidden;
      if (el.hidden && !hide) comeBack.push(w.id);
      el.hidden = hide;
      el.dataset.size = w.size.toLowerCase();
      const sp = LC.spanFor(r.sizes, w.size, cols);
      el.style.gridColumn = `span ${sp.cols}`;                 // inline: overrides the old .wide / .tall classes
      ensureChip(el, r);
      cardSizes.observe(el);
    }
    LGRID.classList.add('masonry', 'dense');
    packCards();
    document.body.classList.remove('layout-pending');
    const loaders = new Set(comeBack.map((id) => LOADERS[id]).filter(Boolean));
    for (const fn of loaders) { try { window[fn]?.(); } catch (e) { console.error(e); } }
    if (typeof drawCharts === 'function') drawCharts();
    if (typeof payload !== 'undefined' && payload) updateChips(payload);
    renderLayoutButton();
  });
}

// ---------- masonry packing (10-19): a grid row was as tall as its tallest card, so short cards left empty space
// below them (a 5,800 px page). The grid's rows are MASONRY_ROW px; each card spans as many as its own height needs,
// and `dense` lets the next card move up into the gap. Re-packed whenever a card's content changes its height.
const MASONRY_ROW = 8, MASONRY_GAP = 16;
let packQueued = false;
function packCards() {
  if (packQueued) return;
  packQueued = true;
  setTimeout(() => {                 // not requestAnimationFrame: browsers pause that in hidden tabs, and a hidden
    packQueued = false;              // dashboard must still be packed when you switch back to it
    for (const el of LGRID.querySelectorAll(':scope > .card')) {
      if (el.hidden) continue;
      const span = LC.rowSpan(el.getBoundingClientRect().height, MASONRY_ROW, MASONRY_GAP);
      if (el.style.gridRow !== `span ${span}`) el.style.gridRow = `span ${span}`;
    }
  }, 0);
}
const cardSizes = new ResizeObserver(packCards);
// ResizeObserver reports when cards change size or when grid recalculates.
addEventListener('hud:render', packCards);

// ---------- XS chips: one line per card, from the live data
function ensureChip(el, r) {
  if (!el.querySelector(':scope > .xs-chip')) {
    const chip = document.createElement('div');
    chip.className = 'xs-chip';
    chip.dataset.title = r.title;
    el.prepend(chip);
  }
  const h2 = el.querySelector(':scope > h2');
  if (h2 && !h2.querySelector('.card-tools')) {
    const tools = document.createElement('span');
    tools.className = 'card-tools';

    // Cycle size button (⤢)
    const resizeBtn = document.createElement('button');
    resizeBtn.type = 'button';
    resizeBtn.className = 'card-tool-btn';
    resizeBtn.title = 'Cycle card size';
    resizeBtn.textContent = '⤢';
    resizeBtn.onclick = (e) => {
      e.stopPropagation();
      cycleCardSize(el, r);
    };

    // Fold / collapse button (▴)
    const fold = document.createElement('button');
    fold.type = 'button';
    fold.className = 'card-tool-btn';
    fold.title = 'Collapse card to compact strip';
    fold.textContent = '▴';
    fold.onclick = (e) => {
      e.stopPropagation();
      toggleCardCollapse(el, r);
    };

    // Pin to top button (📌)
    const pinBtn = document.createElement('button');
    pinBtn.type = 'button';
    pinBtn.className = 'card-tool-btn';
    pinBtn.title = 'Pin card to top';
    pinBtn.textContent = '📌';
    pinBtn.onclick = (e) => {
      e.stopPropagation();
      pinCardToTop(el);
    };

    tools.appendChild(resizeBtn);
    tools.appendChild(fold);
    tools.appendChild(pinBtn);
    h2.appendChild(tools);
  }
}
function cycleCardSize(el, r) {
  if (!el || EDIT || !r?.sizes) return;
  const avail = Object.keys(r.sizes); // e.g. ["XS", "S", "M", "L", "XL"]
  if (avail.length <= 1) return;
  const cur = (el.dataset.size || 'm').toUpperCase();
  const idx = avail.indexOf(cur);
  const next = avail[(idx + 1) % avail.length];
  withViewTransition(() => {
    el.dataset.size = next.toLowerCase();
    const sp = LC.spanFor(r.sizes, next, gridCols());
    el.style.gridColumn = `span ${sp.cols}`;
    if (next === 'XS' && typeof payload !== 'undefined' && payload) updateChips(payload);
    packCards();
  });
}
function pinCardToTop(el) {
  if (!el || EDIT || !LGRID) return;
  withViewTransition(() => {
    LGRID.prepend(el);
    packCards();
    el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  });
}
function toggleCardCollapse(el, r) {
  if (!el || EDIT) return;
  withViewTransition(() => {
    if (el.dataset.size === 'xs' && el.dataset.prevSize) {
      const restore = el.dataset.prevSize;
      delete el.dataset.prevSize;
      el.dataset.size = restore;
      const sp = LC.spanFor(r.sizes, restore.toUpperCase(), gridCols());
      el.style.gridColumn = `span ${sp.cols}`;
    } else if (el.dataset.size !== 'xs') {
      el.dataset.prevSize = el.dataset.size || (r.default || 'M').toLowerCase();
      el.dataset.size = 'xs';
      el.style.gridColumn = 'span 1';
      if (typeof payload !== 'undefined' && payload) updateChips(payload);
    }
    packCards();
  });
}
document.addEventListener('click', (e) => {
  const chip = e.target.closest('.card[data-size="xs"] > .xs-chip');
  if (chip && !EDIT) {
    const el = chip.parentElement;
    const r = reg(el?.id);
    if (el && r && el.dataset.prevSize) toggleCardCollapse(el, r);
  }
});
const pct = (v) => (v == null ? '–' : `${Math.round(v)}%`);
const kbs = (v) => (v == null ? '–' : v >= 1024 ? `${(v / 1024).toFixed(1)} MB/s` : `${Math.round(v)} KB/s`);
const CHIPS = {
  'c-gpu': (s) => `CPU <b>${pct(s.cpu_percent)}</b> · GPU <b>${pct(s.gpu_load)}</b>${s.gpu_temp != null ? ` · ${s.gpu_temp}°C` : ''}`,
  'c-vram': (s) => `<b>${s.vram_used_gb != null ? s.vram_used_gb.toFixed(1) : '–'}</b> / ${Math.round(s.vram_total_gb || 16)} GB · `
    + (s.vram_spill_impact === 'slow' ? '<span style="color:var(--red)">spilled</span>' : s.vram_tight ? '<span style="color:var(--amber)">tight</span>' : 'ok'),
  'c-ai': (s) => `<b>${esc(s.ai_model || 'no model')}</b> · ${esc(s.ai_state || s.ai_mode || '')}${s.ai_tps ? ` · ${Math.round(s.ai_tps)} tok/s` : ''}`,
  'c-ai-thinking': () => {
    const ops = window.payload?.ai_ops || {};
    const th = ops.thinking || {};
    const sp = ops.speed || {};
    if (th.active) return `<b style="color:var(--purple)">Thinking</b> · ${sp.tg_tps || '–'} tok/s`;
    if (ops.active) return `<b style="color:var(--cyan)">Generating</b> · ${sp.tg_tps || '–'} tok/s`;
    return `Idle${ops.model ? ` · ${esc(ops.model)}` : ''}`;
  },
  'c-awayq': (s) => (s.ai_mode === 'away' ? `<b style="color:var(--cyan)">Away</b> · ${esc(s.away_line || '')}` : 'Away off'),
  'c-system': (s) => `RAM <b>${pct(s.ram_percent)}</b> · ↓ ${kbs(s.net_down_kb)} · ↑ ${kbs(s.net_up_kb)}`,
  'c-disks': (s) => {
    const d = LC.fullestDisk(s.disks);
    return d ? `${esc(d.drive)}: <b>${d.free_gb} GB</b> free · the fullest` : 'disks –';
  },
  'c-stab': (s) => { const n = (s.unexpected_reboots || 0) + (s.gpu_resets || 0);
    return `crashes <b style="color:${n ? 'var(--red)' : 'var(--green)'}">${n}</b> · backup ${s.backup_age_h != null ? `${Math.round(s.backup_age_h)} h` : '–'}`; },
  'c-net': (s) => (s.net_alert ? `<b style="color:var(--red)">${esc(s.net_alert)}</b>`
    : `Pi-hole <b style="color:var(--green)">ok</b>${s.net_new_device ? ` · <span style="color:var(--amber)">${esc(s.net_new_device)}</span>` : ''}`),
  'c-svc': (s) => Object.entries(s.services || {}).map(([k, v]) => `<span style="color:${v ? 'var(--green)' : 'var(--red)'}">●</span> ${esc(k)}`).join(' &nbsp;'),
  'c-health': () => `<b>${esc(lcard('healthPill')?.textContent || '…')}</b>`,
  'c-work': (s) => `note <b>${s.note_words ?? '–'}</b> words · ${s.git_dirty_count ?? '–'} repos changed`,
  'c-chains': (s) => {
    if (s.chain_running) return `<b style="color:var(--cyan)">${esc(s.chain_running)}</b>${s.chain_step ? ` · ${esc(s.chain_step)}` : ''}`;
    if (s.chain_pending_count) return `idle · <b>${s.chain_pending_count}</b> waiting`;
    if (s.chain_next) return `idle · next: <b>${esc(s.chain_next)}</b>`;
    return 'idle';
  },
  'c-projects': (s) => {
    const apps = s.apps_running || [];
    return apps.length ? `<b>${apps.length} running</b>: ${esc(apps.slice(0, 2).join(', '))}${apps.length > 2 ? '…' : ''}` : 'nothing running';
  },
  'c-media': (s) => {
    const med = s.media_running || [];
    if (med.length) return `<b>${med.length} active</b> (${esc(med.join(', '))})`;
    if (s.media_playing) return `playing: <b>${esc(s.media_title || '')}</b>`;
    return 'all idle';
  },
  'c-ports': () => {
    const count = lcard('portsSummary')?.textContent?.trim() || '';
    return count ? `<b>${esc(count)}</b>` : 'local listeners';
  },
  'c-notes': (s) => {
    const sum = lcard('notesSummary')?.textContent?.trim() || '';
    return sum ? `<b>${esc(sum)}</b>` : (s.note_words ? `daily note: <b>${s.note_words} words</b>` : 'no edits yet');
  },
  'c-week': () => {
    const h = lcard('weekHours')?.textContent?.trim() || '';
    return h ? `<b>${esc(h)}</b> this week` : 'weekly activity';
  },
  'c-activity': (s, p) => {
    const acts = p?.activity || [];
    const first = acts[0];
    return first ? `<span style="color:${first.color || 'var(--dim)'}">${esc(first.text)}</span>` : 'no recent activity';
  },
  'c-ask': () => {
    const c = lcard('askPendingCount')?.textContent?.trim();
    return c ? `<b>${esc(c)}</b> waiting` : 'idle';
  },
  'c-solang': () => 'speed reader & vocab',
  'c-review': (s) => {
    const r = s.review || {};
    return r.active ? `<b style="color:var(--cyan)">active</b> · ${esc(r.title || '')}` : 'idle';
  },
  'c-security': () => {
    const pill = lcard('secPill')?.textContent?.trim() || 'ok';
    const ipad = lcard('secIpadStatus')?.textContent?.trim() || '';
    return `<b>${esc(pill)}</b>${ipad ? ` · <span class="dim">${esc(ipad)}</span>` : ''}`;
  },
  'c-pad': () => {
    const pill = lcard('padPill')?.textContent?.trim() || 'offline';
    const usb = lcard('padUsb')?.textContent?.trim() || '';
    return `<b>${esc(pill)}</b>${usb ? ` · <span class="dim">${esc(usb)}</span>` : ''}`;
  },
  'c-overnight': () => {
    const st = lcard('overnightStatusPill')?.textContent?.trim() || 'idle';
    return `<b>${esc(st)}</b>`;
  },
  'c-ai-usage': () => {
    const w = lcard('aiUsageWeek')?.textContent?.trim() || 'Claude & Antigravity';
    return `<b>${esc(w)}</b>`;
  },
  'c-playground': () => {
    const st = lcard('pgStatus')?.textContent?.trim() || 'ready';
    return `<b style="color:var(--cyan)">Console</b> · ${esc(st)}`;
  },
  'c-procs': () => {
    const sum = lcard('procSummary')?.textContent?.trim() || 'radar';
    return `<b>Procs</b> · ${esc(sum)}`;
  },
  'c-media-ctl': () => {
    const t = lcard('mediaDeckTitle')?.textContent?.trim() || 'no media';
    return `<b>Audio</b> · ${esc(t)}`;
  },
};
function updateChips(p) {
  const s = p?.snapshot || {};
  for (const el of LGRID.querySelectorAll('.card[data-size="xs"]')) {
    const chip = el.querySelector(':scope > .xs-chip'); const fn = CHIPS[el.id];
    if (chip && fn) { try { setHTML(chip, `<span class="dim">${esc(chip.dataset.title)}</span> ${fn(s, p)}`); } catch (e) { console.error(e); } }
  }
}

// ---------- loading, switching, following the server
async function loadLayouts() {
  try { LAYOUT = await (await fetch('/api/layouts', { cache: 'no-store' })).json(); } catch (e) { document.body.classList.remove('layout-pending'); return; }
  if (!EDIT) applyPreset(activePreset());
}
async function layoutPost(body) {
  const r = await post('/api/layouts', body);
  if (r && r.presets) { LAYOUT = r; if (!EDIT) applyPreset(activePreset()); }
  return r;
}
async function activateLayout(name) {
  closeLayoutMenu();
  const r = await layoutPost({ action: 'activate', name });
  toast(r?.why || (r?.ok ? name : 'that did not work'));
}
addEventListener('hud:render', (e) => {
  const p = e.detail;
  if (p?.layout && LAYOUT && !EDIT && (p.layout.rev !== LAYOUT.rev || p.layout.active !== LAYOUT.active)) loadLayouts();
  updateChips(p);
});
// the grid's own width decides how many columns a span may take (window resize, zoom, a narrower pane)
let lastCols = 0;
new ResizeObserver(() => {
  const c = gridCols();
  if (c === lastCols || !LAYOUT) return;
  lastCols = c;
  applyPreset(EDIT ? EDIT.preset : activePreset());
}).observe(LGRID);

// ---------- the button and its menu
const layoutsBtn = document.createElement('button');
layoutsBtn.id = 'layoutsBtn'; layoutsBtn.className = 'mini'; layoutsBtn.title = 'Layouts: switch and arrange the cards (Alt+1…9)';
lcard('settingsBtn')?.after(layoutsBtn);
const layoutMenu = document.createElement('div');
layoutMenu.className = 'lmenu'; layoutMenu.id = 'layoutMenu'; layoutMenu.hidden = true;
document.body.appendChild(layoutMenu);

function renderLayoutButton() {
  const p = activePreset();
  const label = document.createElement('span');                  // hidden on narrower screens: the icon stays
  label.className = 'lbl'; label.textContent = ` ${p ? p.name : 'Layouts'}`;
  layoutsBtn.replaceChildren(p?.icon || '▦', label);
  layoutsBtn.title = `Layout: ${p ? p.name : 'none'}. Switch and arrange the cards (Alt+1…9)`;
  renderQuickbarPresets();
  applyQuickFilters();
}

// ---------- Quick Switcher Bar (below header): presets, category filters, card search, jump menu, compact mode
const CARD_CATS = {
  hw: ['c-gpu', 'c-vram', 'c-system', 'c-disks'],
  ai: ['c-ai', 'c-ai-thinking', 'c-chains', 'c-overnight', 'c-ask', 'c-awayq', 'c-ai-usage'],
  dev: ['c-projects', 'c-ports', 'c-work', 'c-notes', 'c-media', 'c-solang', 'c-review'],
  sys: ['c-security', 'c-net', 'c-pad', 'c-svc', 'c-stab', 'c-health', 'c-week', 'c-activity'],
};
let activeQuickCat = 'all';
let compactAllActive = false;

function renderQuickbarPresets() {
  const box = lcard('qbPresets');
  if (!box || !LAYOUT) return;
  const html = (LAYOUT.presets || []).map((p, i) => {
    const on = p.name === LAYOUT.active ? ' on' : '';
    const count = p.widgets.filter((w) => !w.hidden).length;
    const tip = `${p.name} (${count} cards)${i < 9 ? ` · Alt+${i + 1}` : ''}`;
    return `<button class="qb-pill${on}" data-qpreset="${esc(p.name)}" title="${esc(tip)}">`
      + `<span class="qb-ic">${esc(p.icon || '▦')}</span><span>${esc(p.name)}</span></button>`;
  }).join('');
  setHTML(box, html);
}

function applyQuickFilters() {
  if (!LAYOUT || EDIT) return;
  const p = activePreset();
  if (!p) return;
  const catSet = activeQuickCat !== 'all' ? new Set(CARD_CATS[activeQuickCat] || []) : null;
  const comeBack = [];

  for (const w of p.widgets) {
    const el = lcard(w.id);
    const r = reg(w.id);
    if (!el || !r) continue;
    let shouldHide = !!w.hidden;
    if (catSet) {
      shouldHide = !catSet.has(w.id);
    }
    if (el.hidden && !shouldHide) comeBack.push(w.id);
    el.hidden = shouldHide;
  }

  const loaders = new Set(comeBack.map((id) => LOADERS[id]).filter(Boolean));
  for (const fn of loaders) { try { window[fn]?.(); } catch (e) { console.error(e); } }
  packCards();
}

function jumpToCard(cardId) {
  const el = lcard(cardId);
  if (!el) return;
  if (el.hidden) {
    if (activeQuickCat !== 'all') {
      activeQuickCat = 'all';
      document.querySelectorAll('#qbCats .qb-pill').forEach((b) => b.classList.toggle('on', b.dataset.qcat === 'all'));
      applyQuickFilters();
    }
    if (el.hidden) {
      el.hidden = false;
      const fn = LOADERS[cardId];
      if (fn) { try { window[fn]?.(); } catch (e) { console.error(e); } }
      packCards();
    }
  }
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'center' });
  el.classList.remove('flash');
  void el.offsetWidth;
  el.classList.add('flash');
}
window.jumpToCard = jumpToCard;

// Quick-bar search (app.js initQuickSearch): cards of the active layout whose title or id contains q.
function quickCardMatches(q, max) {
  q = String(q || '').toLowerCase().trim();
  const p = activePreset();
  if (!q || !p) return [];
  const out = [];
  for (const w of p.widgets) {
    const r = reg(w.id);
    if (!r) continue;
    if (r.title.toLowerCase().includes(q) || w.id.toLowerCase().includes(q)) {
      out.push({ id: w.id, title: r.title, hidden: !!lcard(w.id)?.hidden });
      if (out.length >= (max || 5)) break;
    }
  }
  return out;
}
window.quickCardMatches = quickCardMatches;

function renderJumpMenu() {
  const menu = lcard('qbJumpMenu');
  if (!menu || !LAYOUT) return;
  const catNames = { hw: '⚡ Hardware', ai: '🧠 AI & Chains', dev: '🛠 Dev & Notes', sys: '🛡 System & Net' };
  const cols = Object.entries(CARD_CATS).map(([cat, ids]) => {
    const btns = ids.map((id) => {
      const r = reg(id);
      if (!r) return '';
      const el = lcard(id);
      const isHidden = el?.hidden;
      return `<button class="qb-jump-item${isHidden ? ' is-hidden' : ''}" data-qjump="${esc(id)}">`
        + `<span>${esc(r.title)}</span>${isHidden ? '<small class="dim">hidden</small>' : ''}</button>`;
    }).join('');
    return `<div class="qb-jump-col"><div class="qb-jump-head">${catNames[cat]}</div>${btns}</div>`;
  }).join('');
  setHTML(menu, `<div class="qb-jump-grid">${cols}</div>`);
}

const qbBar = lcard('quickbar');
if (qbBar) {
  qbBar.addEventListener('click', (e) => {
    const presetBtn = e.target.closest('[data-qpreset]');
    if (presetBtn) {
      activateLayout(presetBtn.dataset.qpreset);
      return;
    }
    const catBtn = e.target.closest('[data-qcat]');
    if (catBtn) {
      const cat = catBtn.dataset.qcat;
      activeQuickCat = (activeQuickCat === cat && cat !== 'all') ? 'all' : cat;
      qbBar.querySelectorAll('#qbCats .qb-pill').forEach((b) => b.classList.toggle('on', b.dataset.qcat === activeQuickCat));
      applyQuickFilters();
      return;
    }
    const jumpItem = e.target.closest('[data-qjump]');
    if (jumpItem) {
      lcard('qbJumpMenu').hidden = true;
      jumpToCard(jumpItem.dataset.qjump);
      return;
    }
  });
}

if (lcard('qbEditLayoutBtn')) {
  lcard('qbEditLayoutBtn').onclick = () => {
    if (EDIT) { endEdit(); applyPreset(activePreset()); toast('edit cancelled'); }
    else startEdit();
  };
}

if (lcard('qbJumpBtn')) {
  lcard('qbJumpBtn').onclick = (e) => {
    e.stopPropagation();
    const menu = lcard('qbJumpMenu');
    if (!menu) return;
    if (menu.hidden) {
      renderJumpMenu();
      menu.hidden = false;
    } else {
      menu.hidden = true;
    }
  };
  document.addEventListener('click', (e) => {
    const menu = lcard('qbJumpMenu');
    if (menu && !menu.hidden && !menu.contains(e.target) && e.target !== lcard('qbJumpBtn')) {
      menu.hidden = true;
    }
  });
}

if (lcard('qbCompactBtn')) {
  lcard('qbCompactBtn').onclick = () => {
    if (EDIT) return;
    compactAllActive = !compactAllActive;
    lcard('qbCompactBtn').classList.toggle('on', compactAllActive);
    const p = activePreset();
    if (!p) return;
    const cols = gridCols();
    for (const w of p.widgets) {
      const el = lcard(w.id);
      const r = reg(w.id);
      if (!el || !r) continue;
      const targetSize = compactAllActive ? 'XS' : w.size;
      delete el.dataset.prevSize;
      el.dataset.size = targetSize.toLowerCase();
      const sp = LC.spanFor(r.sizes, targetSize, cols);
      el.style.gridColumn = `span ${sp.cols}`;
    }
    if (typeof payload !== 'undefined' && payload) updateChips(payload);
    packCards();
  };
}

addEventListener('keydown', (e) => {
  if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey) {
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '')) return;
    const flt = lcard('qbFilter');
    if (flt) {
      e.preventDefault();
      flt.focus();
      flt.select();
    }
  }
});
function renderLayoutMenu() {
  const items = (LAYOUT?.presets || []).map((p, i) => `<button class="litem${p.name === LAYOUT.active ? ' on' : ''}" data-lname="${esc(p.name)}">`
    + `<span class="lic">${esc(p.icon || '▦')}</span><span class="lname">${esc(p.name)}</span>`
    + `<span class="dim small">${p.widgets.filter((w) => !w.hidden).length} cards</span>`
    + `${p.auto_when ? `<span class="dim small">auto: ${esc(p.auto_when)}</span>` : ''}`
    + `${i < 9 ? `<kbd>Alt+${i + 1}</kbd>` : ''}</button>`).join('');
  setHTML(layoutMenu, items + '<hr>'
    + '<button class="litem" data-lact="edit">✎ Edit this layout</button>'
    + '<button class="litem" data-lact="saveas">＋ Save as new preset…</button>'
    + '<button class="litem" data-lact="manage">⚙ Manage presets…</button>'
    + '<button class="litem" data-lact="reset_defaults" title="Reset all presets to curated defaults">↺ Reset to curated presets</button>'
    + `<label class="litem lauto"><input type="checkbox" class="switch" id="layoutAuto"${LAYOUT?.auto ? ' checked' : ''}> Auto-switch for games and Away</label>`);
}
function openLayoutMenu() {
  renderLayoutMenu();
  const r = layoutsBtn.getBoundingClientRect();
  layoutMenu.style.top = `${r.bottom + 6}px`;
  layoutMenu.style.left = `${Math.max(8, Math.min(r.left, innerWidth - 300))}px`;
  layoutMenu.hidden = false;
}
function closeLayoutMenu() { layoutMenu.hidden = true; }
layoutsBtn.onclick = (e) => { e.stopPropagation(); if (layoutMenu.hidden) openLayoutMenu(); else closeLayoutMenu(); };
document.addEventListener('click', (e) => { if (!layoutMenu.hidden && !layoutMenu.contains(e.target) && e.target !== layoutsBtn) closeLayoutMenu(); });
layoutMenu.addEventListener('click', async (e) => {
  const item = e.target.closest('[data-lname]');
  if (item) return activateLayout(item.dataset.lname);
  const act = e.target.closest('[data-lact]')?.dataset.lact;
  if (act === 'edit') { closeLayoutMenu(); startEdit(); }
  if (act === 'saveas') {
    closeLayoutMenu();
    const name = prompt('Name for the new preset (a copy of the current layout):', `${LAYOUT.active} copy`);
    if (!name) return;
    const base = JSON.parse(JSON.stringify(activePreset()));
    const r = await layoutPost({ action: 'save', preset: { ...base, name, auto_when: null } });
    if (r?.ok) await activateLayout(name); else toast(r?.why || 'that did not work');
  }
  if (act === 'manage') { closeLayoutMenu(); openManage(); }
  if (act === 'reset_defaults') {
    closeLayoutMenu();
    if (confirm('Reset all presets to the curated defaults (Gaming, AI work, Dev, Overnight, Everyday, Minimal)?')) {
      const r = await layoutPost({ action: 'reset_defaults' });
      if (r?.presets) toast('Reset to curated presets');
      else toast(r?.why || 'Could not reset presets');
    }
  }
});
layoutMenu.addEventListener('change', async (e) => {
  if (e.target.id === 'layoutAuto') { const r = await layoutPost({ action: 'auto', on: e.target.checked }); toast(r?.why || ''); }
});

// Alt+1…9 switch presets, Alt+E toggles Edit mode (not while typing)
addEventListener('keydown', (e) => {
  if (e.altKey && !e.ctrlKey && !e.metaKey && (e.key === 'e' || e.key === 'E')) {
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '')) return;
    e.preventDefault();
    closeLayoutMenu();
    if (EDIT) { endEdit(); applyPreset(activePreset()); toast('edit cancelled'); }
    else { startEdit(); }
    return;
  }
  if (!e.altKey || e.ctrlKey || e.metaKey || EDIT) return;
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '')) return;
  const n = Number(e.key);
  const p = LAYOUT?.presets?.[n - 1];
  if (n >= 1 && n <= 9 && p) { e.preventDefault(); activateLayout(p.name); }
});

// Edit mode keyboard shortcuts (Ctrl+S / Cmd+S, Enter inside input)
addEventListener('keydown', (e) => {
  if (!EDIT) return;
  if ((e.ctrlKey || e.metaKey) && (e.key === 's' || e.key === 'S')) {
    e.preventDefault();
    editBar.querySelector('#leDone')?.click();
    return;
  }
  if (e.key === 'Enter' && (e.target.id === 'leName' || e.target.id === 'leIcon')) {
    e.preventDefault();
    editBar.querySelector('#leDone')?.click();
  }
});

// the Ctrl+K palette (app.js asks for these)
function layoutPaletteItems() {
  const items = (LAYOUT?.presets || []).map((p) => ({ label: `Layout: ${p.name}`, cat: 'Layout', run: () => { closePalette(); activateLayout(p.name); } }));
  items.push({ label: 'Edit this layout', cat: 'Layout', run: () => { closePalette(); startEdit(); } });
  return items;
}

// ---------- edit mode
const editBar = document.createElement('div');
editBar.className = 'leditbar'; editBar.hidden = true;
document.body.appendChild(editBar);
const tray = document.createElement('div');
tray.className = 'ltray'; tray.hidden = true;
LGRID.after(tray);

function startEdit() {
  const p = activePreset();
  if (!p) return;
  EDIT = { old: p.name, preset: JSON.parse(JSON.stringify(p)) };
  document.body.classList.add('layout-editing');
  for (const w of EDIT.preset.widgets) { const el = lcard(w.id); if (el) addEditBar(el); }
  renderEditBar();
  redrawEdit();
}
function addEditBar(el) {
  if (el.querySelector(':scope > .lbar')) return;
  const bar = document.createElement('div');
  bar.className = 'lbar';
  el.prepend(bar);
}
function redrawEdit() {
  applyPreset(EDIT.preset);
  for (const w of EDIT.preset.widgets) {
    const el = lcard(w.id); const r = reg(w.id);
    if (!el || !r) continue;
    const bar = el.querySelector(':scope > .lbar');
    const sizes = LC.sizesOf(r.sizes);
    setHTML(bar, `<button class="lhandle" draggable="true" title="Drag to move (or focus and use the arrow keys)" data-lmove="${esc(w.id)}">⠿</button>`
      + `<span class="ltitle">${esc(r.title)}</span>`
      + `<span class="lsizes">${sizes.map((s) => `<button class="${s === w.size ? 'on' : ''}" data-lsize="${s}" data-lid="${esc(w.id)}" title="size ${s}">${s}</button>`).join('')}</span>`
      + `<button class="lhide" data-lhide="${esc(w.id)}" title="Hide this card (it goes to the tray below)">👁</button>`);
  }
  const visible = EDIT.preset.widgets.filter((w) => !w.hidden);
  let emptyBox = LGRID.querySelector('.lempty-notice');
  if (!visible.length) {
    if (!emptyBox) {
      emptyBox = document.createElement('div');
      emptyBox.className = 'lempty-notice';
      emptyBox.innerHTML = '<b>All cards hidden.</b><br><span class="dim">Click cards in the tray below to show them.</span>';
      LGRID.appendChild(emptyBox);
    }
  } else if (emptyBox) {
    emptyBox.remove();
  }
  const hidden = EDIT.preset.widgets.filter((w) => w.hidden);
  tray.hidden = false;
  setHTML(tray, `<h3>Hidden in “${esc(EDIT.preset.name)}” <span class="dim">click to add back</span></h3>`
    + (hidden.length
      ? `<div class="ltray-btns"><button class="mini go" id="ltrayShowAll" title="Unhide all cards at once">＋ Show all (${hidden.length})</button> `
        + hidden.map((w) => `<button class="mini" data-lshow="${esc(w.id)}">＋ ${esc(reg(w.id)?.title || w.id)}</button>`).join(' ')
        + `</div>`
      : '<span class="dim">nothing hidden</span>'));
}
function wIndex(id) { return EDIT.preset.widgets.findIndex((w) => w.id === id); }
function moveWidget(id, to) {
  EDIT.preset.widgets = LC.moveTo(EDIT.preset.widgets, id, to);
  redrawEdit();
}
LGRID.addEventListener('click', (e) => {
  if (!EDIT) return;
  const sz = e.target.closest('[data-lsize]');
  if (sz) { const w = EDIT.preset.widgets[wIndex(sz.dataset.lid)]; w.size = sz.dataset.lsize; redrawEdit(); return; }
  const hd = e.target.closest('[data-lhide]');
  if (hd) { EDIT.preset.widgets[wIndex(hd.dataset.lhide)].hidden = true; redrawEdit(); }
});
LGRID.addEventListener('keydown', (e) => {
  const h = e.target.closest?.('[data-lmove]');
  if (!EDIT || !h) return;
  const id = h.dataset.lmove;
  const dir = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[e.key];
  if (!dir) return;
  e.preventDefault();
  const to = LC.neighbourIndex(EDIT.preset.widgets, id, dir);     // past hidden cards
  if (to >= 0) { moveWidget(id, to); lcard(id)?.querySelector('[data-lmove]')?.focus(); }
});
tray.addEventListener('click', (e) => {
  if (e.target.id === 'ltrayShowAll' || e.target.closest('#ltrayShowAll')) {
    if (!EDIT) return;
    for (const w of EDIT.preset.widgets) w.hidden = false;
    redrawEdit();
    return;
  }
  const b = e.target.closest('[data-lshow]');
  if (!b || !EDIT) return;
  EDIT.preset.widgets = LC.showAtEnd(EDIT.preset.widgets, b.dataset.lshow);   // back at the end, where you can see it
  redrawEdit();
});
let dragId = null;
LGRID.addEventListener('dragstart', (e) => {
  const h = e.target.closest?.('[data-lmove]');
  if (!EDIT || !h) { if (EDIT) e.preventDefault(); return; }
  dragId = h.dataset.lmove;
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', dragId);
  lcard(dragId)?.classList.add('ldragging');
});
LGRID.addEventListener('dragover', (e) => {
  if (!EDIT || !dragId) return;
  const over = e.target.closest('.card');
  if (!over || over.id === dragId) return;
  e.preventDefault();
  for (const c of LGRID.querySelectorAll('.ldrop-before, .ldrop-after')) c.classList.remove('ldrop-before', 'ldrop-after');
  const r = over.getBoundingClientRect();
  over.classList.add(e.clientX < r.left + r.width / 2 ? 'ldrop-before' : 'ldrop-after');
});
LGRID.addEventListener('drop', (e) => {
  if (!EDIT || !dragId) return;
  e.preventDefault();
  const over = e.target.closest('.card');
  if (over && over.id !== dragId) {
    EDIT.preset.widgets = LC.dropReorder(EDIT.preset.widgets, dragId, over.id, over.classList.contains('ldrop-before'));
  }
  endDrag(); redrawEdit();
});
LGRID.addEventListener('dragend', () => endDrag());
function endDrag() {
  for (const c of LGRID.querySelectorAll('.ldrop-before, .ldrop-after, .ldragging')) c.classList.remove('ldrop-before', 'ldrop-after', 'ldragging');
  dragId = null;
}

function renderEditBar() {
  const p = EDIT.preset;
  const starter = ['Everyday', 'Gaming', 'AI work', 'Dev', 'Overnight', 'Minimal'].includes(EDIT.old);
  const slides = LAYOUT.slides || [];
  const own = Array.isArray(p.ticker_slides);
  const wasOpen = editBar.querySelector('.lslides')?.open;
  const gameHold = (LAYOUT.presets || []).find((x) => x.name !== p.name && x.auto_when === 'game');
  const awayHold = (LAYOUT.presets || []).find((x) => x.name !== p.name && x.auto_when === 'away');
  const gameTxt = gameHold ? `while a game runs (replaces ${gameHold.name})` : 'while a game runs';
  const awayTxt = awayHold ? `while Away runs (replaces ${awayHold.name})` : 'while Away runs';
  setHTML(editBar, `<b>Editing</b>`
    + `<input id="leName" maxlength="40" value="${esc(p.name)}" title="Name">`
    + `<input id="leIcon" maxlength="4" value="${esc(p.icon || '')}" title="Icon (an emoji or a character)" class="lshort">`
    + `<select id="leAuto" title="Switch to this layout by itself"><option value="">no auto-switch</option>`
    + `<option value="game"${p.auto_when === 'game' ? ' selected' : ''}>${esc(gameTxt)}</option>`
    + `<option value="away"${p.auto_when === 'away' ? ' selected' : ''}>${esc(awayTxt)}</option></select>`
    + `<details class="lslides"${wasOpen ? ' open' : ''}><summary>Ticker: ${own ? `${p.ticker_slides.length} slides` : 'my Settings'}</summary>`
    + `<div class="lslidepanel"><label><input type="checkbox" id="leOwnSlides"${own ? ' checked' : ''}> this layout picks the ticker's slides</label>`
    + `<div class="lslidelist">${slides.map((s) => `<label><input type="checkbox" data-lslide="${esc(s.tag)}"${own && p.ticker_slides.includes(s.tag) ? ' checked' : ''}${own ? '' : ' disabled'}> ${esc(s.label)}</label>`).join('')}</div></div></details>`
    + `<span class="lgrow"></span>`
    + (starter ? '<button class="mini ghost" id="leReset" title="Back to how this starter preset shipped">Reset</button>' : '')
    + '<button class="mini ghost" id="leCancel">Cancel</button><button class="mini go" id="leDone">Done</button>');
  editBar.hidden = false;
}
editBar.addEventListener('change', (e) => {
  if (!EDIT) return;
  const p = EDIT.preset;
  if (e.target.id === 'leOwnSlides') {
    p.ticker_slides = e.target.checked ? (LAYOUT.slides || []).map((s) => s.tag).slice(0, 4) : null;
    renderEditBar();
  } else if (e.target.dataset.lslide) {
    const on = [...editBar.querySelectorAll('[data-lslide]:checked')].map((x) => x.dataset.lslide);
    if (!on.length) { e.target.checked = true; toast('the ticker needs at least one slide'); return; }
    p.ticker_slides = on;
  }
});
editBar.addEventListener('click', async (e) => {
  if (!EDIT) return;
  if (e.target.id === 'leCancel') { endEdit(); applyPreset(activePreset()); }
  if (e.target.id === 'leReset') {
    if (!confirm(`Reset “${EDIT.old}” to how it shipped?`)) return;
    const r = await layoutPost({ action: 'reset', name: EDIT.old });
    endEdit(); applyPreset(activePreset()); toast(r?.why || '');
  }
  if (e.target.id === 'leDone') {
    const p = EDIT.preset;
    const visible = p.widgets.filter((w) => !w.hidden);
    if (!visible.length) { toast('A layout needs at least one visible card'); return; }
    p.name = editBar.querySelector('#leName').value.trim();
    p.icon = editBar.querySelector('#leIcon').value.trim();
    p.auto_when = editBar.querySelector('#leAuto').value || null;
    const old = EDIT.old;
    const r = await post('/api/layouts', { action: 'save', old, preset: p });
    if (!r?.ok) { toast(r?.why || 'that did not work'); return; }
    LAYOUT = r; endEdit(); applyPreset(activePreset()); toast(r.why);
  }
});
function endEdit() {
  EDIT = null;
  document.body.classList.remove('layout-editing');
  LGRID.querySelector('.lempty-notice')?.remove();
  for (const b of LGRID.querySelectorAll('.lbar')) b.remove();
  editBar.hidden = true; tray.hidden = true;
}

// ---------- manage presets: order, delete, reset
const manage = document.createElement('div');
manage.className = 'modal'; manage.hidden = true;
manage.innerHTML = '<div class="sheet lmanage"><header><h2>Layout presets</h2>'
  + '<div class="lmheader-btns"><button class="mini ghost" id="lmExport" title="Copy all presets to clipboard as JSON">Export JSON</button>'
  + '<button class="mini ghost" id="lmImport" title="Import presets from JSON">Import JSON</button>'
  + '<button class="mini ghost" id="lmResetAll" title="Reset all presets to curated defaults">↺ Reset Curated Defaults</button>'
  + '<button class="mini ghost" data-lclose>✕</button></div></header><div id="lmanageList"></div>'
  + '<div class="note">The first nine are Alt+1…Alt+9. Starter presets can be reset to how they shipped.</div></div>';
document.body.appendChild(manage);
function openManage() { renderManage(); manage.hidden = false; }
function renderManage() {
  const ps = LAYOUT.presets || [];
  const starters = ['Everyday', 'Gaming', 'AI work', 'Dev', 'Overnight', 'Minimal'];
  setHTML(manage.querySelector('#lmanageList'), ps.map((p, i) => `<div class="srow"><span><b>${esc(p.icon || '▦')} ${esc(p.name)}</b>`
    + `<span class="dim small">${p.widgets.filter((w) => !w.hidden).length} cards${p.auto_when ? ` · auto: ${esc(p.auto_when)}` : ''}${p.ticker_slides ? ` · ticker: ${p.ticker_slides.length} slides` : ''}</span></span>`
    + `<span class="lmbtns"><button class="mini ghost" data-lup="${esc(p.name)}" data-to="${i - 1}"${i === 0 ? ' disabled' : ''}>↑</button>`
    + `<button class="mini ghost" data-lup="${esc(p.name)}" data-to="${i + 1}"${i === ps.length - 1 ? ' disabled' : ''}>↓</button>`
    + `<button class="mini ghost" data-lcopy="${esc(p.name)}" title="Duplicate this preset">Copy</button>`
    + (starters.includes(p.name) ? `<button class="mini ghost" data-lreset="${esc(p.name)}">Reset</button>` : '')
    + `<button class="mini danger" data-ldel="${esc(p.name)}"${ps.length === 1 ? ' disabled' : ''}>Delete</button></span></div>`).join(''));
}
manage.addEventListener('click', async (e) => {
  if (e.target === manage || e.target.closest('[data-lclose]')) { manage.hidden = true; return; }
  if (e.target.id === 'lmExport' || e.target.closest('#lmExport')) {
    const text = JSON.stringify(LAYOUT.presets, null, 2);
    try {
      await navigator.clipboard.writeText(text);
      toast('Presets copied to clipboard as JSON');
    } catch {
      prompt('Presets JSON:', text);
    }
    return;
  }
  if (e.target.id === 'lmImport' || e.target.closest('#lmImport')) {
    let clip = '';
    try { clip = await navigator.clipboard.readText(); } catch {}
    const prefill = (clip && (clip.trim().startsWith('{') || clip.trim().startsWith('['))) ? clip : '';
    const input = prompt('Paste presets JSON (single preset or array):', prefill);
    if (!input) return;
    try {
      const parsed = JSON.parse(input);
      const list = Array.isArray(parsed) ? parsed : [parsed];
      const r = await layoutPost({ action: 'import', presets: list });
      if (r?.ok) { toast(r.why); renderManage(); }
      else { toast(r?.why || 'Import failed'); }
    } catch (err) {
      toast(`Invalid JSON: ${err.message}`);
    }
    return;
  }
  if (e.target.id === 'lmResetAll' || e.target.closest('#lmResetAll')) {
    if (confirm('Reset all presets to the curated defaults (Gaming, AI work, Dev, Overnight, Everyday, Minimal)?')) {
      const r = await layoutPost({ action: 'reset_defaults' });
      if (r?.presets) { toast('Reset to curated presets'); renderManage(); }
      else { toast(r?.why || 'Could not reset presets'); }
    }
    return;
  }
  const cp = e.target.closest('[data-lcopy]');
  if (cp) {
    const srcName = cp.dataset.lcopy;
    const src = (LAYOUT.presets || []).find((p) => p.name === srcName);
    if (!src) return;
    let newName = `${srcName} copy`;
    let n = 2;
    const names = (LAYOUT.presets || []).map((p) => p.name);
    while (names.includes(newName)) { newName = `${srcName} copy ${n++}`; }
    const r = await layoutPost({ action: 'save', preset: { ...JSON.parse(JSON.stringify(src)), name: newName, auto_when: null } });
    if (r?.ok) { toast(`Copied to “${newName}”`); renderManage(); }
    else { toast(r?.why || 'could not duplicate'); }
    return;
  }
  const up = e.target.closest('[data-lup]');
  if (up) { await layoutPost({ action: 'move', name: up.dataset.lup, to: Number(up.dataset.to) }); renderManage(); }
  const rs = e.target.closest('[data-lreset]');
  if (rs && confirm(`Reset “${rs.dataset.lreset}” to how it shipped?`)) { const r = await layoutPost({ action: 'reset', name: rs.dataset.lreset }); toast(r?.why || ''); renderManage(); }
  const del = e.target.closest('[data-ldel]');
  if (del && confirm(`Delete the preset “${del.dataset.ldel}”?`)) { const r = await layoutPost({ action: 'delete', name: del.dataset.ldel }); toast(r?.why || ''); renderManage(); }
});
addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (!manage.hidden) { manage.hidden = true; return; }
    if (!layoutMenu.hidden) { closeLayoutMenu(); return; }
    if (EDIT) { endEdit(); applyPreset(activePreset()); toast('edit cancelled'); return; }
  }
});

document.body.classList.add('layout-pending');
setTimeout(() => document.body.classList.remove('layout-pending'), 1500);   // never leave the page blank
loadLayouts();
