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
  'c-health': 'loadHealth', 'c-projects': 'loadProjects', 'c-media': 'loadMedia', 'c-chains': 'loadControl', 'c-ask': 'loadControl',
  'c-awayq': 'loadControl' };

function cardShown(id) { const el = lcard(id); return !el || !el.hidden; }      // app.js / launchpad.js ask this

function gridCols() {
  const t = getComputedStyle(LGRID).gridTemplateColumns || '';
  return Math.max(1, t.split(' ').filter(Boolean).length);
}
function reg(id) { return (LAYOUT?.widgets || []).find((w) => w.id === id); }
function activePreset() { return (LAYOUT?.presets || []).find((p) => p.name === LAYOUT.active) || LAYOUT?.presets?.[0]; }

// ---------- applying a preset to the page
function applyPreset(p) {
  if (!p) return;
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
    el.style.gridRow = `span ${sp.rows}`;
    ensureChip(el, r);
  }
  LGRID.classList.toggle('dense', p.name !== 'Everyday');
  document.body.classList.remove('layout-pending');
  const loaders = new Set(comeBack.map((id) => LOADERS[id]).filter(Boolean));
  for (const fn of loaders) { try { window[fn]?.(); } catch (e) { console.error(e); } }
  if (typeof drawCharts === 'function') drawCharts();
  if (typeof payload !== 'undefined' && payload) updateChips(payload);
  renderLayoutButton();
}

// ---------- XS chips: one line per card, from the live data
function ensureChip(el, r) {
  if (el.querySelector(':scope > .xs-chip')) return;
  const chip = document.createElement('div');
  chip.className = 'xs-chip';
  chip.dataset.title = r.title;
  el.prepend(chip);
}
const pct = (v) => (v == null ? '–' : `${Math.round(v)}%`);
const kbs = (v) => (v == null ? '–' : v >= 1024 ? `${(v / 1024).toFixed(1)} MB/s` : `${Math.round(v)} KB/s`);
const CHIPS = {
  'c-gpu': (s) => `CPU <b>${pct(s.cpu_percent)}</b> · GPU <b>${pct(s.gpu_load)}</b>${s.gpu_temp != null ? ` · ${s.gpu_temp}°C` : ''}`,
  'c-vram': (s) => `<b>${s.vram_used_gb != null ? s.vram_used_gb.toFixed(1) : '–'}</b> / ${Math.round(s.vram_total_gb || 16)} GB · `
    + (s.vram_spill_impact === 'slow' ? '<span style="color:var(--red)">spilled</span>' : s.vram_tight ? '<span style="color:var(--amber)">tight</span>' : 'ok'),
  'c-ai': (s) => `<b>${esc(s.ai_model || 'no model')}</b> · ${esc(s.ai_state || s.ai_mode || '')}${s.ai_tps ? ` · ${Math.round(s.ai_tps)} tok/s` : ''}`,
  'c-awayq': (s) => (s.ai_mode === 'away' ? `<b style="color:var(--cyan)">Away</b> · ${esc(s.away_line || '')}` : 'Away off'),
  'c-system': (s) => `RAM <b>${pct(s.ram_percent)}</b> · ↓ ${kbs(s.net_down_kb)} · ↑ ${kbs(s.net_up_kb)}`,
  'c-disks': (s) => {
    const d = LC.fullestDisk(s.disks);
    return d ? `${esc(d.drive)}: <b>${d.free_gb} GB</b> free · the fullest` : 'disks –';
  },
  'c-stab': (s) => { const n = (s.unexpected_reboots || 0) + (s.gpu_resets || 0);
    return `crashes <b style="color:${n ? 'var(--red)' : 'var(--green)'}">${n}</b> · backup ${s.backup_age_h != null ? `${Math.round(s.backup_age_h)} h` : '–'}`; },
  'c-svc': (s) => Object.entries(s.services || {}).map(([k, v]) => `<span style="color:${v ? 'var(--green)' : 'var(--red)'}">●</span> ${esc(k)}`).join(' &nbsp;'),
  'c-health': () => `<b>${esc(lcard('healthPill')?.textContent || '…')}</b>`,
  'c-work': (s) => `note <b>${s.note_words ?? '–'}</b> words · ${s.git_dirty_count ?? '–'} repos changed`,
};
function updateChips(p) {
  const s = p?.snapshot || {};
  for (const el of LGRID.querySelectorAll('.card[data-size="xs"]')) {
    const chip = el.querySelector(':scope > .xs-chip'); const fn = CHIPS[el.id];
    if (chip && fn) { try { setHTML(chip, `<span class="dim">${esc(chip.dataset.title)}</span> ${fn(s)}`); } catch (e) { console.error(e); } }
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
  layoutsBtn.textContent = `▦ ${p ? `${p.icon ? `${p.icon} ` : ''}${p.name}` : 'Layouts'}`;
}
function renderLayoutMenu() {
  const items = (LAYOUT?.presets || []).map((p, i) => `<button class="litem${p.name === LAYOUT.active ? ' on' : ''}" data-lname="${esc(p.name)}">`
    + `<span class="lic">${esc(p.icon || '▦')}</span><span class="lname">${esc(p.name)}</span>`
    + `${p.auto_when ? `<span class="dim small">auto: ${esc(p.auto_when)}</span>` : ''}`
    + `${i < 9 ? `<kbd>Alt+${i + 1}</kbd>` : ''}</button>`).join('');
  setHTML(layoutMenu, items + '<hr>'
    + '<button class="litem" data-lact="edit">✎ Edit this layout</button>'
    + '<button class="litem" data-lact="saveas">＋ Save as new preset…</button>'
    + '<button class="litem" data-lact="manage">⚙ Manage presets…</button>'
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
});
layoutMenu.addEventListener('change', async (e) => {
  if (e.target.id === 'layoutAuto') { const r = await layoutPost({ action: 'auto', on: e.target.checked }); toast(r?.why || ''); }
});

// Alt+1…9 switch presets (not while typing)
addEventListener('keydown', (e) => {
  if (!e.altKey || e.ctrlKey || e.metaKey || EDIT) return;
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '')) return;
  const n = Number(e.key);
  const p = LAYOUT?.presets?.[n - 1];
  if (n >= 1 && n <= 9 && p) { e.preventDefault(); activateLayout(p.name); }
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
  const hidden = EDIT.preset.widgets.filter((w) => w.hidden);
  tray.hidden = false;
  setHTML(tray, `<h3>Hidden in “${esc(EDIT.preset.name)}” <span class="dim">click to add back</span></h3>`
    + (hidden.length ? hidden.map((w) => `<button class="mini" data-lshow="${esc(w.id)}">＋ ${esc(reg(w.id)?.title || w.id)}</button>`).join(' ') : '<span class="dim">nothing hidden</span>'));
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
  setHTML(editBar, `<b>Editing</b>`
    + `<input id="leName" maxlength="40" value="${esc(p.name)}" title="Name">`
    + `<input id="leIcon" maxlength="4" value="${esc(p.icon || '')}" title="Icon (an emoji or a character)" class="lshort">`
    + `<select id="leAuto" title="Switch to this layout by itself"><option value="">no auto-switch</option>`
    + `<option value="game"${p.auto_when === 'game' ? ' selected' : ''}>while a game runs</option>`
    + `<option value="away"${p.auto_when === 'away' ? ' selected' : ''}>while Away runs</option></select>`
    + `<details class="lslides"><summary>Ticker: ${own ? `${p.ticker_slides.length} slides` : 'my Settings'}</summary>`
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
  for (const b of LGRID.querySelectorAll('.lbar')) b.remove();
  editBar.hidden = true; tray.hidden = true;
}

// ---------- manage presets: order, delete, reset
const manage = document.createElement('div');
manage.className = 'modal'; manage.hidden = true;
manage.innerHTML = '<div class="sheet lmanage"><header><h2>Layout presets</h2><button class="mini ghost" data-lclose>✕</button></header><div id="lmanageList"></div>'
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
    + (starters.includes(p.name) ? `<button class="mini ghost" data-lreset="${esc(p.name)}">Reset</button>` : '')
    + `<button class="mini danger" data-ldel="${esc(p.name)}"${ps.length === 1 ? ' disabled' : ''}>Delete</button></span></div>`).join(''));
}
manage.addEventListener('click', async (e) => {
  if (e.target === manage || e.target.closest('[data-lclose]')) { manage.hidden = true; return; }
  const up = e.target.closest('[data-lup]');
  if (up) { await layoutPost({ action: 'move', name: up.dataset.lup, to: Number(up.dataset.to) }); renderManage(); }
  const rs = e.target.closest('[data-lreset]');
  if (rs && confirm(`Reset “${rs.dataset.lreset}” to how it shipped?`)) { const r = await layoutPost({ action: 'reset', name: rs.dataset.lreset }); toast(r?.why || ''); renderManage(); }
  const del = e.target.closest('[data-ldel]');
  if (del && confirm(`Delete the preset “${del.dataset.ldel}”?`)) { const r = await layoutPost({ action: 'delete', name: del.dataset.ldel }); toast(r?.why || ''); renderManage(); }
});
addEventListener('keydown', (e) => { if (e.key === 'Escape') { if (!manage.hidden) manage.hidden = true; else closeLayoutMenu(); } });

document.body.classList.add('layout-pending');
setTimeout(() => document.body.classList.remove('layout-pending'), 1500);   // never leave the page blank
loadLayouts();
