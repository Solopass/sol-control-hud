// SOL Control HUD dashboard. Data: /api/stream (pushed every 2 s while this tab is visible; a hidden tab closes it,
// so the hub slows down), /api/history (the last hour for the charts), /api/meta (themes). Buttons POST with the
// X-SOL-Control header (the server refuses anything else). No libraries: charts are canvas, animations are CSS.
'use strict';
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const COLOR = { '#38bdf8': 'var(--cyan)', '#4ade80': 'var(--green)', '#fbbf24': 'var(--amber)', '#f87171': 'var(--red)',
                '#94a3b8': 'var(--dim)', '#e2e8f0': 'var(--text)' };
const col = (hex) => COLOR[hex] || hex || 'var(--text)';
const segHtml = (segs) => (segs || []).map(([t, c]) => `<span style="color:${col(c)}">${esc(t)}</span>`).join('');
const slide = (p, tag) => (p.slides || []).find((s) => s.tag === tag);
const REDUCE = matchMedia('(prefers-reduced-motion: reduce)').matches;
const CARD_FOR = { SYS: 'c-stab', HW: 'c-gpu', DISK: 'c-disks', RUN: 'c-chains', AI: 'c-ai', NIGHT: 'c-awayq', GIT: 'c-work', NOTE: 'c-work' };
const hist = { t: [], gpu: [], vram: [], ram: [], cpu: [], temp: [], hotspot: [], down: [], up: [] };
let range = '1h';
const SPAN = { '1h': 3600, '24h': 86400, '7d': 604800 };
let last = 0, es = null, themes = {}, themeName = null, payload = null, seenFeed = new Set(), firstFeed = true;

// ---------- small helpers
async function post(url, body) {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-SOL-Control': '1' }, body: JSON.stringify(body) });
  return r.json();
}
function toast(text) {
  const t = document.createElement('div');
  t.className = 'toast';
  if (typeof t.showPopover === 'function') t.setAttribute('popover', 'manual');
  t.setAttribute('role', 'status');
  t.textContent = text;
  document.body.append(t);
  if (typeof t.showPopover === 'function') { try { t.showPopover(); } catch {} }
  setTimeout(() => {
    if (typeof t.hidePopover === 'function') { try { t.hidePopover(); } catch {} }
    t.remove();
  }, 3200);
}
let _activeConfirmClose = null;
function hudConfirm(text, confirmLabel = 'Confirm') {
  if (_activeConfirmClose) _activeConfirmClose(false);
  return new Promise((resolve) => {
    const t = document.createElement('div');
    t.className = 'toast toast-confirm';
    if (typeof t.showPopover === 'function') t.setAttribute('popover', 'manual');
    t.setAttribute('role', 'region');
    t.setAttribute('aria-label', 'Confirmation');

    const msg = document.createElement('span');
    msg.className = 'toast-msg';
    msg.textContent = text;

    const actions = document.createElement('span');
    actions.className = 'toast-actions';

    const okBtn = document.createElement('button');
    okBtn.type = 'button';
    okBtn.className = 'mini go';
    okBtn.textContent = confirmLabel;

    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'mini ghost';
    cancelBtn.textContent = 'Cancel';

    actions.append(okBtn, cancelBtn);
    t.append(msg, actions);
    document.body.append(t);
    if (typeof t.showPopover === 'function') { try { t.showPopover(); } catch {} }

    let timer = null;
    let settled = false;
    const finish = (val) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (_activeConfirmClose === finish) _activeConfirmClose = null;
      document.removeEventListener('keydown', onKey);
      if (typeof t.hidePopover === 'function') { try { t.hidePopover(); } catch {} }
      t.remove();
      resolve(val);
    };
    _activeConfirmClose = finish;

    const armTimer = () => {
      clearTimeout(timer);
      timer = setTimeout(() => finish(false), 8000);
    };
    t.addEventListener('focusin', () => clearTimeout(timer));
    t.addEventListener('focusout', armTimer);
    t.addEventListener('mouseenter', () => clearTimeout(timer));
    t.addEventListener('mouseleave', armTimer);
    armTimer();

    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    };
    document.addEventListener('keydown', onKey);

    okBtn.onclick = () => finish(true);
    cancelBtn.onclick = () => finish(false);
  });
}
function hudPrompt(text, defaultValue = '') {
  return new Promise((resolve) => {
    const dlg = document.createElement('dialog');
    dlg.className = 'hud-dialog';
    dlg.setAttribute('aria-labelledby', 'hudPromptTitle');

    const form = document.createElement('form');
    form.method = 'dialog';

    const title = document.createElement('div');
    title.id = 'hudPromptTitle';
    title.className = 'hud-dialog-title';
    title.textContent = text;

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'hud-dialog-input';
    input.value = defaultValue;
    input.autofocus = true;

    const btns = document.createElement('div');
    btns.className = 'hud-dialog-btns';

    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'mini ghost';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.onclick = () => { dlg.close(); };

    const okBtn = document.createElement('button');
    okBtn.type = 'submit';
    okBtn.className = 'mini go';
    okBtn.value = 'ok';
    okBtn.textContent = 'OK';

    btns.append(cancelBtn, okBtn);
    form.append(title, input, btns);
    dlg.append(form);
    document.body.append(dlg);

    dlg.addEventListener('close', () => {
      const val = dlg.returnValue === 'ok' ? input.value : null;
      dlg.remove();
      resolve(val);
    });
    dlg.showModal();
    input.select();
  });
}
function tween(el, to, digits = 0) {
  if (to == null || Number.isNaN(to)) { el.textContent = '–'; el._v = null; return; }
  const from = el._v ?? to; el._v = to;
  if (REDUCE || from === to) { el.textContent = to.toFixed(digits); return; }
  const t0 = performance.now(), dur = 700;
  const step = (now) => {
    const k = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - k, 3);
    el.textContent = (from + (to - from) * e).toFixed(digits);
    if (k < 1 && el._v === to) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
function setHTML(el, html) { if (el._html !== html) { el.innerHTML = html; el._html = html; } }
function setBar(el, frac, color) {
  const i = el.querySelector('i'); i.style.width = `${Math.max(0, Math.min(1, frac || 0)) * 100}%`;
  if (color) i.style.background = color;
}
const hhmm = (iso) => (iso || '').slice(11, 16);
function when(iso) {
  if (!iso) return '';
  const d = new Date(iso), today = new Date();
  return d.toDateString() === today.toDateString() ? iso.slice(11, 16) : `${d.toLocaleDateString(undefined, { weekday: 'short' })} ${iso.slice(11, 16)}`;
}
function nextLabel(iso) {
  if (iso === 'on away') return 'when Away starts';
  const d = new Date(iso); if (Number.isNaN(+d)) return iso;
  const days = Math.round((new Date(d.toDateString()) - new Date(new Date().toDateString())) / 864e5);
  return `${days === 0 ? 'today' : days === 1 ? 'tomorrow' : d.toLocaleDateString(undefined, { weekday: 'long' })} ${iso.slice(11, 16)}`;
}

// ---------- theme (shared with the ticker)
function applyTheme(name) {
  const t = themes[name]; if (!t || name === themeName) return;
  themeName = name; $('theme').value = name;
  const r = document.documentElement.style;
  const map = { bg: 'bg', border: 'line', card: 'card', badge_bg: 'badge', text_main: 'text', text_muted: 'muted', text_dim: 'dim',
                accent_primary: 'cyan', accent_green: 'green', accent_amber: 'amber', accent_red: 'red' };
  for (const [k, v] of Object.entries(map)) if (t[k]) r.setProperty(`--${v}`, t[k]);
  drawCharts();
}

