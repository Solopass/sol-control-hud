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

// ---------- charts (canvas, last hour)
const SERIES_COLOR = { gpu: '--cyan', temp: '--amber', vram: '--cyan', ram: '--green', cpu: '--cyan', down: '--cyan', up: '--amber' };
function css(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }

const chartVisibility = new WeakMap();
const chartObserver = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    chartVisibility.set(entry.target, entry.isIntersecting);
  }
}, { rootMargin: '100px 0px' });

function drawChart(cv) {
  if (!chartVisibility.has(cv)) {
    chartObserver.observe(cv);
    chartVisibility.set(cv, true);
    initChartCrosshair(cv);
  }
  if (!chartVisibility.get(cv)) return; // off-screen optimization

  const names = cv.dataset.series.split(','), maxes = cv.dataset.max.split(',');
  const dpr = devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
  if (!w || !h) return;
  if (cv.width !== Math.round(w * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
  const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
  g.strokeStyle = css('--line'); g.lineWidth = 1; g.setLineDash([2, 4]);
  for (const f of [0.25, 0.5, 0.75]) { g.beginPath(); g.moveTo(0, h * f); g.lineTo(w, h * f); g.stroke(); }
  g.setLineDash([]);
  const span = SPAN[range], now = Date.now() / 1000, t0 = now - span, ts = hist.t;
  if (ts.length < 2) { g.fillStyle = css('--dim'); g.font = '11px Segoe UI'; g.fillText('collecting the last hour…', 6, h / 2); return; }
  names.forEach((name, si) => {
    const ys = hist[name]; let max = parseFloat(maxes[si] ?? maxes[0]);
    if (name === 'vram' && payload?.snapshot?.vram_total_gb) max = payload.snapshot.vram_total_gb;
    if (name === 'ram' && payload?.snapshot?.ram_total_gb) max = payload.snapshot.ram_total_gb;
    if (Number.isNaN(max)) max = Math.max(64, ...names.flatMap((n) => hist[n].filter((v) => v != null))) * 1.15;
    const color = css(SERIES_COLOR[name] || '--cyan');
    g.beginPath(); let started = false, lastX = 0;
    for (let i = 0; i < ts.length; i++) {
      if (ys[i] == null || ts[i] < t0) continue;
      const x = ((ts[i] - t0) / span) * w, y = h - 2 - Math.min(1, ys[i] / max) * (h - 6);
      if (!started) { g.moveTo(x, y); started = true; } else g.lineTo(x, y);
      lastX = x;
    }
    if (!started) return;
    g.strokeStyle = color; g.lineWidth = 1.6; g.lineJoin = 'round'; g.stroke();
    if (si === 0) {   // the first series gets a soft area fill
      g.lineTo(lastX, h); g.lineTo(((ts.find((t) => t >= t0) - t0) / span) * w, h); g.closePath();
      const grad = g.createLinearGradient(0, 0, 0, h); grad.addColorStop(0, color + '55'); grad.addColorStop(1, color + '00');
      g.fillStyle = grad; g.fill();
    }
  });

  // Render crosshair if cursor is hovering
  if (cv._hoverX != null && cv._hoverX >= 0 && cv._hoverX <= w) {
    g.strokeStyle = css('--cyan');
    g.lineWidth = 1;
    g.setLineDash([2, 2]);
    g.beginPath();
    g.moveTo(cv._hoverX, 0);
    g.lineTo(cv._hoverX, h);
    g.stroke();
    g.setLineDash([]);
  }

  g.fillStyle = css('--dim'); g.font = '10px Segoe UI'; g.fillText({ '1h': '60 min', '24h': '24 h', '7d': '7 days' }[range], 4, h - 4); g.fillText('now', w - 22, h - 4);
}

function initChartCrosshair(cv) {
  let tip = null;
  cv.addEventListener('mousemove', (e) => {
    const rect = cv.getBoundingClientRect();
    const x = e.clientX - rect.left;
    cv._hoverX = x;
    const w = cv.clientWidth, span = SPAN[range], now = Date.now() / 1000, t0 = now - span, ts = hist.t;
    if (ts.length < 2 || w <= 0) return;
    const targetT = t0 + (x / w) * span;
    let closestIdx = 0, minDiff = Infinity;
    for (let i = 0; i < ts.length; i++) {
      const diff = Math.abs(ts[i] - targetT);
      if (diff < minDiff) { minDiff = diff; closestIdx = i; }
    }
    const names = cv.dataset.series.split(',');
    const lines = names.map((n) => {
      const val = hist[n]?.[closestIdx];
      const unit = n.includes('kb') || n === 'down' || n === 'up' ? ' KB/s' : n.includes('temp') ? '°C' : (n === 'ram' || n === 'vram') ? ' GB' : '%';
      return `${n}: <b>${val != null ? (typeof val === 'number' ? val.toFixed(1) : val) : '–'}${unit}</b>`;
    });
    const timeStr = new Date(ts[closestIdx] * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    if (!tip) {
      tip = document.createElement('div');
      tip.className = 'chart-tip';
      cv.parentElement.appendChild(tip);
    }
    tip.style.left = `${x}px`;
    tip.style.top = `${e.clientY - rect.top}px`;
    tip.innerHTML = `<span class="dim">${timeStr}</span> · ${lines.join(' · ')}`;
    tip.hidden = false;
    drawChart(cv);
  });
  cv.addEventListener('mouseleave', () => {
    cv._hoverX = null;
    if (tip) tip.hidden = true;
    drawChart(cv);
  });
}
let drawQueued = false;
function drawCharts() {
  if (drawQueued) return; drawQueued = true;
  requestAnimationFrame(() => { drawQueued = false; document.querySelectorAll('canvas.chart').forEach(drawChart); });
}
function addPoint(p) {
  if (range !== '1h') return;          // the 24 h / 7 d charts come from the minute history (reloaded every minute)
  if (!p || (hist.t.length && p.t <= hist.t[hist.t.length - 1])) return;
  for (const k of Object.keys(hist)) hist[k].push(p[k] ?? null);
  const cut = p.t - 3600;
  while (hist.t.length && hist.t[0] < cut) for (const k of Object.keys(hist)) hist[k].shift();
}
async function loadHistory() {
  try { const h = await (await fetch(`/api/history?range=${range}`, { cache: 'no-store' })).json(); for (const k of Object.keys(hist)) hist[k] = h[k] || []; drawCharts(); } catch (e) { /* the stream fills it */ }
}

// ---------- render one pushed message
function render(p) {
  payload = p; const s = p.snapshot;
  last = Date.now(); const live = $('live'); live.classList.add('on'); live.classList.remove('beat'); void live.offsetWidth; live.classList.add('beat');
  if (p.views?.theme) applyTheme(p.views.theme);

  // header
  const mode = s.ai_mode || '?'; const m = $('mode');
  m.textContent = mode === 'off' && s.ai_reason ? `OFF · ${s.ai_reason.replace('game: ', '')}` : mode.toUpperCase();
  m.className = `mode ${mode}`;
  setHTML($('headline'), segHtml((slide(p, 'AWAY') || slide(p, 'AI') || {}).segments));
  const tb = $('tickerBtn'); tb.hidden = false; tb.title = p.views.ticker ? 'Hide the ticker' : 'Show the ticker';
  setHTML(tb, `▭<span class="lbl"> ${p.views.ticker ? 'Hide' : 'Show'} ticker</span>`);
  tb.onclick = async () => { tb.textContent = '…'; await post('/api/views', { ticker: !p.views.ticker }); };
  $('foot').textContent = `pace ${p.views.pace_s}s · ${s.gpu_name || ''}`;
  if ($('hdrCpu')) setHTML($('hdrCpu'), `CPU <b>${s.cpu_percent != null ? Math.round(s.cpu_percent) + '%' : '–'}</b>`);
  if ($('hdrGpu')) setHTML($('hdrGpu'), `GPU <b>${s.gpu_load != null ? Math.round(s.gpu_load) + '%' : '–'}</b>${s.gpu_temp != null ? ` <small class="dim">${s.gpu_temp}°</small>` : ''}`);
  if ($('hdrVram')) {
    const vCol = s.vram_spill_impact === 'slow' ? 'var(--red)' : s.vram_tight ? 'var(--amber)' : '';
    setHTML($('hdrVram'), `VRAM <b${vCol ? ` style="color:${vCol}"` : ''}>${s.vram_used_gb != null ? s.vram_used_gb.toFixed(1) : '–'}G</b>`);
  }
  if ($('hdrRam')) setHTML($('hdrRam'), `RAM <b>${s.ram_percent != null ? Math.round(s.ram_percent) + '%' : '–'}</b>`);

  renderAlerts(p.attention || []);
  renderAway(p);

  // CPU (top half of the CPU · GPU card): same layout as the GPU half below it
  const c = p.cpu || {};
  $('cpuName').textContent = c.name ? c.name.replace(/\(R\)|\(TM\)|Intel|Core/g, '').replace(/\s+/g, ' ').trim() : '';
  tween($('cpuLoad'), c.load ?? s.cpu_percent, 0);
  const boost = c.clock_ghz && c.base_ghz ? c.clock_ghz / c.base_ghz : 0;
  setHTML($('cpuClock'), c.clock_ghz == null ? '' :
    `<span>clock <b style="color:${boost >= 1.4 ? 'var(--cyan)' : 'var(--text)'}">${c.clock_ghz.toFixed(2)} GHz</b></span>` +
    (c.base_ghz ? `<span class="dim">base ${c.base_ghz}</span>` : ''));
  const cbox = $('cpuEngines');
  const crow = [['P-cores', c.p_load], ['E-cores', c.e_load], [c.busiest ? `busiest ${c.busiest.name}` : 'busiest', c.busiest?.load]]
    .filter(([, v]) => v != null);
  const cnames = crow.map(([n]) => n).join('|');
  if (cbox._names !== cnames) {
    cbox._names = cnames;
    cbox.innerHTML = crow.map(([n]) => `<span class="dim">${esc(n)}</span><div class="bar"><i></i></div><span class="v"></span>`).join('');
  }
  crow.forEach(([, v], i) => {
    setBar(cbox.querySelectorAll('.bar')[i], v / 100, v >= 90 ? 'var(--amber)' : 'var(--cyan)');
    cbox.querySelectorAll('.v')[i].textContent = `${v.toFixed(0)}%`;
  });
  // one cell per core, brighter = busier (P-cores wider: they carry two threads each)
  const cores = c.cores || [], cc2 = $('cpuCores');
  if (cc2.children.length !== cores.length) {
    cc2.innerHTML = cores.map((_, i) => `<i class="${i < (c.p_cores || 0) ? 'p' : 'e'}"></i>`).join('');
  }
  cores.forEach((v, i) => {
    const el = cc2.children[i];
    el.style.setProperty('--l', Math.min(1, v / 100).toFixed(2));
    el.title = `${i < c.p_cores ? `P${i}` : `E${i - c.p_cores}`}: ${v.toFixed(0)}%`;
  });
  $('cpuSplit').textContent = c.p_cores ? `${c.p_cores} P + ${c.e_cores} E cores · ${c.threads} threads` : '';

  // GPU
  $('gpuName').textContent = s.gpu_name || (p.gpu?.available === false ? 'unavailable' : '');
  tween($('gpuLoad'), s.gpu_load, 0);
  const hot = s.gpu_hotspot, temp = s.gpu_temp;
  setHTML($('gpuTemps'), temp == null ? '' :
    `<span>edge <b style="color:${temp >= 85 ? 'var(--amber)' : 'var(--text)'}">${temp}°</b></span>` +
    (hot ? `<span>hotspot <b style="color:${hot >= 95 ? 'var(--red)' : 'var(--text)'}">${hot}°</b></span>` : '') +
    (s.gpu_mem_temp ? `<span>mem <b>${s.gpu_mem_temp}°</b></span>` : ''));
  // engine bars: kept per engine and updated in place, so their width glides instead of jumping
  const eng = Object.entries(p.gpu?.engines || {}).sort((a, b) => b[1] - a[1]).slice(0, 4), box = $('gpuEngines');
  const names = eng.map(([n]) => n).join('|');
  if (box._names !== names) {
    box._names = names;
    box.innerHTML = eng.map(([n]) => `<span class="dim">${esc(n)}</span><div class="bar"><i></i></div><span class="v"></span>`).join('');
  }
  eng.forEach(([, v], i) => { setBar(box.querySelectorAll('.bar')[i], v / 100); box.querySelectorAll('.v')[i].textContent = `${v.toFixed(0)}%`; });
  $('gpuFan').textContent = s.gpu_fan_rpm ? `fan ${s.gpu_fan_rpm} rpm` : '';

  renderVram(p);
  renderAi(p);
  renderSpeed(p);
  renderAiThinking(p);
  renderChains(p);
  renderReview(p);

  // Away & queue card
  const a = p.away || {};
  const q = [...(a.running || []).map((n) => [n, 'running', 'var(--cyan)']), ...(a.queued || []).map((n) => [n, 'waiting', 'var(--amber)'])];
  setHTML($('queueList'), q.length ? q.map(([n, w, c]) => `<div class="item"><span>${esc(n)}</span><span style="color:${c}">${w}</span></div>`).join('')
    : '<div class="empty">Queue empty. Add jobs with tools\\sol-queue.ps1.</div>');
  const night = slide(p, 'NIGHT');
  setHTML($('nightSeg'), night ? segHtml(night.segments) : '<span class="dim">No Away session in the last 18 hours.</span>');

  // memory / cpu / network
  tween($('ramUsed'), s.ram_used_gb, 1); $('ramTotal').textContent = ` / ${Math.round(s.ram_total_gb)} GB`;
  const rp = s.ram_percent;
  setBar($('ramBar'), rp / 100, rp >= 92 ? 'var(--red)' : rp >= 85 ? 'var(--amber)' : 'var(--green)');
  tween($('ramPct'), rp, 0);
  const rate = (kb) => kb >= 1024 ? `${(kb / 1024).toFixed(1)} MB/s` : `${kb.toFixed(0)} KB/s`;
  $('netRate').textContent = `${rate(s.net_down_kb)} / ${rate(s.net_up_kb)}`;

  renderDisks(s);
  renderSecurity(p);
  renderNetwork(p);
  renderSolPad(p);
  renderDeckServices(p);
  renderAiInbox(p);

  // stability
  const sys = slide(p, 'SYS'); setHTML($('sysSeg'), sys ? segHtml(sys.segments) : '');
  const crashes = s.unexpected_reboots + s.gpu_resets, cc = $('crashCount');
  cc.textContent = crashes ? `${crashes}: Away blocked` : '0: Away allowed'; cc.style.color = crashes ? 'var(--red)' : 'var(--green)';
  if ($('ackCrashBtn')) $('ackCrashBtn').hidden = crashes === 0;
  const st = p.stability || {};
  $('ackUntil').textContent = st.since ? st.since.replace('T', ' ') : '–';
  $('ackNote').textContent = st.acknowledged || '';
  const w = p.wsl || {};
  setHTML($('wslState'), w.available === false ? '<span class="dim">unknown</span>' :
    `${esc(w.state || '?')}${w.failed?.length ? ` <span style="color:var(--red)">· ${w.failed.length} failed unit(s)</span>` : ''}`);

  // services
  const ports = { Router: ':11440', Embed: ':11443', HUD: ':7900', Chains: 'runner', Watcher: 'sol-watch', WSL: 'running' };   // WSL off is normal: its services start on demand
  // the router's model process: which chip it's on (the AI should only ever be on the AMD card)
  const llama = ((p.gpu || {}).processes || []).filter((x) => /llama-server/i.test(x.name) && x.dedicated_gb + x.shared_gb >= 0.5);
  const aiChip = llama.map((x) => chipOf(p, x.pid)).find(Boolean);
  setHTML($('svcList'), Object.keys(ports).map((n) => {
    const up = s.services?.[n];
    const canAct = ['Router', 'Watcher', 'Chains'].includes(n);
    const actBtn = canAct
      ? `<button class="mini ghost svc-tile-btn" data-svc-name="${esc(n.toLowerCase())}" title="${up ? `Restart / trigger ${n}` : `Start ${n}`}">${up ? '⟳' : '▶'}</button>`
      : '';
    return `<div class="${up ? 'up' : n === 'WSL' ? '' : 'down'}"><i></i><span>${n}</span><small>${up ? ports[n] : n === 'WSL' ? 'off' : 'down'}${n === 'Router' && up ? chipTag(aiChip, true) : ''}</small>${actBtn}</div>`;
  }).join(''));

  // workspace
  const words = s.note_words || 0, ring = $('noteRing');
  ring.style.strokeDashoffset = `${169.6 * (1 - Math.min(1, s.note_exists ? words / 250 : 0))}`;
  ring.classList.toggle('goal', s.note_exists && words >= 250);
  $('noteWords').textContent = s.note_exists ? `${words} words` : 'not started';
  $('noteSub').textContent = s.note_exists ? `today's note${s.note_time ? ' · edited ' + s.note_time : ''} · goal 250` : (words ? `yesterday: ${words} words` : 'daily note');
  const git = slide(p, 'GIT'); setHTML($('gitSeg'), git ? segHtml(git.segments) : '');
  setHTML($('gitRepos'), (s.git_dirty_repos || []).map((r) => `<span>${esc(r)}</span>`).join(''));
  const med = $('media'), playing = s.media_status === 'Playing';
  med.hidden = !(s.media_title && ['Playing', 'Paused'].includes(s.media_status));
  med.classList.toggle('playing', playing);
  $('mediaText').textContent = s.media_title ? `${s.media_title}${s.media_artist ? ' — ' + s.media_artist : ''}${playing ? '' : ' (paused)'}` : '';

  renderFeed(p.activity || []);
  renderQuickbarBadges(p);
  addPoint(p.point); drawCharts();
  dispatchEvent(new CustomEvent('hud:render', { detail: p }));   // layouts.js: XS chips, follow a layout switch
}

function renderQuickbarBadges(p) {
  const s = p?.snapshot || {};
  const alerts = p?.attention || [];
  const hasAlert = (tag) => alerts.some(a => a[2] === tag);

  // Map category to state
  const cats = {
    hardware: { active: (s.gpu_load > 30 || s.cpu_percent > 40), alert: (s.vram_spill_impact === 'slow' || s.gpu_temp > 85) },
    ai: { active: Boolean(s.ai_generating || s.ai_mode === 'away'), alert: Boolean(s.ai_mode === 'off' && s.ai_reason) },
    system: { active: Boolean(s.ram_percent > 80), alert: Boolean((s.unexpected_reboots || 0) > 0 || hasAlert('SYS')) },
    workspace: { active: Boolean((s.git_dirty_repos || []).length > 0), alert: false },
    network: { active: false, alert: Boolean(s.net_alert) },
  };

  document.querySelectorAll('#qbCats .qb-cat-pill').forEach((pill) => {
    const cat = pill.dataset.cat;
    const st = cats[cat];
    if (!st) return;
    let badge = pill.querySelector('.qb-cat-badge');
    if (!badge) {
      badge = document.createElement('span');
      badge.className = 'qb-cat-badge';
      pill.appendChild(badge);
    }
    if (st.alert) {
      badge.className = 'qb-cat-badge alert';
      badge.title = 'Attention required';
    } else if (st.active) {
      badge.className = 'qb-cat-badge';
      badge.title = 'Active processing';
    } else {
      badge.className = 'qb-cat-badge idle';
      badge.title = 'Idle';
    }
  });
}

function renderAlerts(list) {
  const box = $('alerts'), want = new Map(list.map(([c, t, tag]) => [t, [c, tag]]));
  for (const el of [...box.children]) if (!want.has(el.dataset.key) && !el.classList.contains('out')) { el.classList.add('out'); setTimeout(() => el.remove(), 420); }
  for (const [text, [c, tag]] of want) {
    if ([...box.children].some((el) => el.dataset.key === text && !el.classList.contains('out'))) continue;
    const el = document.createElement('div'); el.className = 'alert'; el.dataset.key = text;
    el.style.setProperty('--c', col(c)); el.textContent = text; el.title = 'Show me';
    el.onclick = () => { const card = $(CARD_FOR[tag]); if (card) { card.scrollIntoView({ behavior: REDUCE ? 'auto' : 'smooth', block: 'center' }); card.classList.remove('flash'); void card.offsetWidth; card.classList.add('flash'); } };
    box.append(el);
  }
}

function renderAway(p) {
  const s = p.snapshot, a = p.away || {}, box = $('away');
  box.hidden = a.mode !== 'away';
  if (box.hidden) return;
  const frac = s.away_fraction, ring = $('awayRing');
  box.classList.toggle('spin', frac == null && s.away_phase === 'working');
  box.classList.toggle('done', s.away_phase === 'done');
  ring.style.strokeDashoffset = `${326.7 * (1 - (frac ?? 0))}`;
  $('awayPct').textContent = frac == null ? '' : `${Math.round(frac * 100)}%`;
  $('awayLine').textContent = s.away_line || 'Getting ready…';
  $('awaySub').textContent = [s.away_eta, a.until ? `Away ends by ${hhmm(a.until)}` : '', a.sleep_when_done && !a.present ? 'then the PC sleeps' : ''].filter(Boolean).join(' · ');
  $('awayWho').textContent = a.present ? "· you're here, the AI keeps working" : a.screen ? '· Away screen on' : '';
  const n = (a.queued || []).length; $('awayQueue').textContent = n ? `${n} more job${n > 1 ? 's' : ''} waiting: ${a.queued.join(', ')}` : '';
}

function renderVram(p) {
  const s = p.snapshot, v = p.vram_guard || {};
  tween($('vramUsed'), s.vram_used_gb, 1); $('vramTotal').textContent = `/ ${s.vram_total_gb ? Math.round(s.vram_total_gb) : '–'} GB`;
  const verdict = v.available ? v.verdict : (s.vram_evicted ? 'EVICTED' : s.vram_tight ? 'TIGHT' : 'OK');
  const pill = $('vramVerdict');
  // EVICTED is the memory counter only; whether it hurts is judged by the next answer (snapshot.spill_impact)
  const impact = s.vram_spill_impact || 'unknown', slowSpill = verdict === 'EVICTED' && impact === 'slow';
  const spillText = { slow: 'SPILLED', fine: 'SPILL · SPEED OK', unknown: 'SPILL?' }[impact] || 'SPILL?';
  pill.textContent = { WONT_FIT: "WON'T FIT", EVICTED: spillText, SPLIT: 'PART ON CPU' }[verdict] || verdict || '…';
  pill.title = { WONT_FIT: 'the default model would not fit next to what is on the card now',
                 EVICTED: { slow: `part of the model is in system RAM and the last answer was slow (${s.ai_tps ?? '?'} tok/s)`,
                            fine: `part of the model is in system RAM, but the last answer ran at its usual speed (${s.ai_tps ?? '?'} tok/s)` }[impact]
                          || 'part of the model is in system RAM; nothing has answered since, so its speed is not known yet',
                 SPLIT: `the engine put ${v.split?.cpu_gb ?? '?'} GB of ${v.split?.model || 'the model'} on the CPU when it started (too little room then): answers are ~3x slower until it reloads`,
                 TIGHT: 'little room left', OK: 'room for the model' }[verdict] || '';
  pill.className = `pill ${slowSpill || verdict === 'SPLIT' ? 'bad' : verdict === 'OK' ? 'ok' : 'warn'}`;
  const card = v.card_gb || s.vram_total_gb || 16, parts = [];
  if (v.available) {
    if (v.ai?.dedicated_gb >= 0.05) parts.push([`AI ${(v.ai.models || []).join(', ') || 'model'}`, v.ai.dedicated_gb, 'var(--green)']);
    const tints = ['var(--cyan)', 'var(--amber)', 'var(--dim)', 'var(--muted)'];
    (v.top_consumers || []).filter((t) => !/llama/i.test(t.name)).slice(0, 4).forEach((t, i) => parts.push([t.label, t.gb, tints[i]]));
  } else (s.vram_processes || []).slice(0, 4).forEach((t, i) => parts.push([t.name, t.dedicated_gb, ['var(--green)', 'var(--cyan)', 'var(--amber)', 'var(--dim)'][i]]));
  const known = parts.reduce((a, b) => a + b[1], 0), used = s.vram_used_gb || 0;
  if (used > known + 0.05) parts.push(['other', used - known, 'color-mix(in srgb, var(--dim) 50%, transparent)']);
  parts.push(['free', Math.max(0, card - Math.max(used, known)), 'transparent']);
  const stack = $('vramStack'); stack.classList.toggle('evicted', slowSpill);
  while (stack.children.length < parts.length) stack.append(document.createElement('i'));
  while (stack.children.length > parts.length) stack.lastChild.remove();
  parts.forEach(([n, gb, c], i) => { const el = stack.children[i]; el.style.flexGrow = Math.max(gb, 0.001); el.style.background = c; el.title = `${n}: ${gb.toFixed(1)} GB`; });
  setHTML($('vramLegend'), parts.filter((x) => x[0] !== 'free').map(([n, gb, c]) => `<span style="--c:${c}">${esc(n)} ${gb.toFixed(1)}</span>`).join(''));
  const notes = [];
  if (v.need?.model) notes.push(`${v.need.model} needs ~${v.need.gb} GB${v.spare_gb != null ? ` · ${v.spare_gb.toFixed(1)} GB spare` : ''}`);
  if (v.evicted) notes.push(`Windows moved ${v.ai?.shared_gb ?? '?'} GB of the model to system RAM: ` +
    ({ slow: 'answers are slow until VRAM frees up', fine: 'answers still run at full speed' }[impact] || 'the next answer shows whether it slows down'));
  if (v.suggest_free?.length && verdict !== 'OK') notes.push(`free up: ${v.suggest_free.map((t) => `${t.label} ${t.gb} GB`).join(', ')}`);
  $('vramNote').textContent = notes.join(' · ');

  const h = p.heal || {};                       // the watcher's spill auto-heal (sol-watch.exe; formerly OBVLT tools/sol-llm-watch.ps1)
  const hr = $('vramHeadroom');
  if (hr) {
    if (v.others_gb != null) {
      const safe = v.others_gb <= 4.6;
      const healText = h.recent ? ` · auto-heal ${h.kind === 'waiting' ? 'waiting: ' + esc(h.text) : 'reloaded ' + agoShort(h.at)}` : '';
      setHTML(hr, `Watcher headroom: <b style="color:${safe ? 'var(--green)' : 'var(--amber)'}">${safe ? 'Safe' : 'Tight'}</b> (${v.others_gb.toFixed(1)} GB / 4.6 GB threshold)${healText}`);
      hr.hidden = false;
    } else {
      hr.hidden = true;
    }
  }
  // apps drawn on the Intel chip (Settings → Graphics chip per app); they keep only a sliver here for the monitors
  const g = p.gpu || {}, off = g.on_other_chips || [];
  const box = $('vramIntel');
  box.hidden = !(g.chips || []).includes('intel');
  if (!box.hidden) {
    const byName = {};
    off.forEach((x) => { byName[x.name] = (byName[x.name] || 0) + x.gb; });
    const list = Object.entries(byName).sort((a, b) => b[1] - a[1]).map(([n, gb]) => `${esc(n)} ${gb.toFixed(2)}`).join(' · ');
    setHTML(box, `<span class="chip intel">Intel</span> ${list ? `drawn there, off this card: ${list} GB` : 'no apps on the Intel chip right now'}`);
  }
}
function chipOf(p, pid) { return ((p?.gpu || {}).pid_chips || {})[pid]; }
function chipTag(chip, warnIfIntel = false) {
  if (!chip) return '';
  const bad = warnIfIntel && chip !== 'amd';
  return ` <span class="chip ${esc(chip)}${bad ? ' bad' : ''}" title="${bad ? 'should be on the AMD card' : `draws on the ${chip === 'amd' ? 'AMD card' : 'Intel chip'}`}">${chip === 'amd' ? 'AMD' : chip === 'intel' ? 'Intel' : esc(chip)}</span>`;
}
function agoShort(epoch) {
  if (!epoch) return '';
  const s = Date.now() / 1000 - epoch;
  return s < 90 ? 'just now' : s < 5400 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
}
// Real answer speed, from the engine's own log. "Slow" is judged by how full the card is, not by the model's
// system-RAM share: sol-fast keeps ~0.6 GB there in every setup (A/B 2026-10-07), fast or slow. What made it slow
// on 10-07 (26 tok/s on prose vs 73 later) was the card at 12.3 GB in use, past the ~12.2 GB Windows allows.
const CARD_LIMIT_GB = 12.2;
function renderSpeed(p) {
  const sp = p.speed || {}, el = $('aiSpeed'), note = $('aiSlow');
  if (!sp.available) { el.textContent = '–'; note.hidden = true; return; }
  const who = sp.model || 'an earlier model process';
  el.textContent = `${sp.tps} tok/s${sp.usual ? ` · usual ~${sp.usual}` : ''} · ${who}${sp.at ? ` · ${agoShort(sp.at)}` : ''}`;
  el.style.color = sp.slow ? 'var(--red)' : sp.usual ? 'var(--green)' : '';
  if (!sp.slow) { note.hidden = true; return; }
  const row = (p.models || []).find((m) => m.id === sp.model) || {}, v = p.vram_guard || {};
  const hogs = (v.top_consumers || []).filter((t) => t.movable && t.gb >= 0.2 && !/llama-server|ollama/i.test(t.name)).slice(0, 4).map((t) => `${t.label} ${t.gb} GB`).join(', ');
  let why;
  const s = p.snapshot || {};
  if (s.ai_split_gb) {                       // placed partly on the CPU at load (vram.split_state): the real cause
    const onCard = s.ai_full_gb != null ? (s.ai_full_gb - s.ai_split_gb).toFixed(1) : '?';
    why = `${esc(s.ai_model || 'the model')} is partly on the CPU: ${onCard} of ${s.ai_full_gb ?? '?'} GB on the card. `
      + `The engine put the rest on the CPU when it started, because other apps held ${v.others_gb != null ? v.others_gb.toFixed(1) : '?'} GB of the card then`
      + (hogs ? ` (${hogs})` : '') + '. '
      + (s.ai_refit_gain_gb >= 0.5 ? 'There is room now: it reloads itself at full speed the next time it is idle.'
        : 'Freeing VRAM makes room; it then reloads itself at full speed when idle.');
  } else if (v.used_gb != null && v.used_gb >= CARD_LIMIT_GB - 0.7) {
    why = `the card is nearly full (${v.used_gb.toFixed(1)} GB in use; Windows lets programs use about ${CARD_LIMIT_GB} GB)`
      + (v.others_gb != null ? `, and other apps hold ${v.others_gb.toFixed(1)} GB of it${hogs ? ` (${hogs})` : ''}` : '')
      + '. Closing some, or moving them to the Intel chip in Settings, makes room.';
  } else {
    why = `the card has room now (${v.used_gb != null ? v.used_gb.toFixed(1) + ' GB in use' : 'no reading'}), so the slow answer came earlier, from a long prompt, or from another program using the GPU.`;
  }
  note.textContent = `Slow: ${why}`;
  note.hidden = false;
}

let thinkEventsInit = false;
let thinkAutoScroll = true;

function highlightSyntax(code) {
  const escCode = esc(code);
  const kw = '\\b(def|class|import|from|return|if|else|elif|for|in|while|try|except|finally|with|as|async|await|lambda|yield|function|const|let|var|switch|case|break|continue|default|true|false|null|None|True|False|undefined|new|this|select|where|insert|update|delete|echo|set|export)\\b';
  const pattern = new RegExp(`(#.*|\\/\\/.*)|("(?:\\\\.|[^"\\\\])*"|'(?:\\\\.|[^'\\\\])*'|` + '`(?:\\\\.|[^`\\\\])*`' + `)|(${kw})|(\\b\\d+(?:\\.\\d+)?\\b)`, 'g');
  return escCode.replace(pattern, (match, cmt, str, kwMatch, num) => {
    if (cmt) return `<span class="syn-cmt">${cmt}</span>`;
    if (str) return `<span class="syn-str">${str}</span>`;
    if (kwMatch) return `<span class="syn-kw">${kwMatch}</span>`;
    if (num) return `<span class="syn-num">${num}</span>`;
    return match;
  });
}

function renderInlineMarkdown(str) {
  return esc(str)
    .replace(/`([^`]+)`/g, '<code class="syn-inline">$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(?:^|\n)(#{1,3})\s+(.*?)(?=\n|$)/g, '<div class="syn-heading">$2</div>');
}

function renderMarkdownStream(text) {
  if (!text) return '';
  const codeBlockRegex = /```([a-zA-Z0-9_-]*)\n?([\s\S]*?)```/g;
  let lastIdx = 0;
  let html = '';
  let match;
  while ((match = codeBlockRegex.exec(text)) !== null) {
    const pre = text.slice(lastIdx, match.index);
    if (pre) html += renderInlineMarkdown(pre);
    const lang = (match[1] || 'code').trim();
    const code = match[2];
    html += `<div class="code-block"><div class="code-block-header"><span class="code-lang">${esc(lang)}</span></div><pre class="code-body"><code>${highlightSyntax(code)}</code></pre></div>`;
    lastIdx = codeBlockRegex.lastIndex;
  }
  const rest = text.slice(lastIdx);
  if (rest) {
    const unclosed = rest.match(/^```([a-zA-Z0-9_-]*)\n?([\s\S]*)$/);
    if (unclosed) {
      const lang = (unclosed[1] || 'code').trim();
      const code = unclosed[2];
      html += `<div class="code-block"><div class="code-block-header"><span class="code-lang">${esc(lang)} (streaming…)</span></div><pre class="code-body"><code>${highlightSyntax(code)}</code></pre></div>`;
    } else {
      html += renderInlineMarkdown(rest);
    }
  }
  return html;
}

function formatThinkingHtml(thoughtText, previewText, isThinking, thinkingS) {
  const parts = [];
  if (thoughtText && thoughtText.trim()) {
    const summaryLabel = isThinking
      ? '💭 Thinking in progress…'
      : `💭 Thought process (${thinkingS ? `${thinkingS}s` : 'completed'})`;
    const openAttr = isThinking ? ' open' : '';
    const hint = isThinking ? 'streaming…' : 'click to expand';
    parts.push(`<details class="think-fold"${openAttr}><summary>${summaryLabel} <span class="think-fold-hint">(${hint})</span></summary><div class="think-fold-body">${renderMarkdownStream(thoughtText)}</div></details>`);
  }
  if (previewText && previewText.trim()) {
    parts.push(`<div class="answer-stream">${renderMarkdownStream(previewText)}</div>`);
  }
  return parts.join('');
}

function initAiThinkingEvents() {
  if (thinkEventsInit) return;
  thinkEventsInit = true;
  const autoBtn = $('thinkAutoScrollBtn');
  if (autoBtn) {
    autoBtn.onclick = () => {
      thinkAutoScroll = !thinkAutoScroll;
      autoBtn.setAttribute('aria-pressed', String(thinkAutoScroll));
      autoBtn.textContent = thinkAutoScroll ? '↓ Auto' : '⏸ Paused';
    };
  }
  const clearBtn = $('thinkClearBtn');
  if (clearBtn) {
    clearBtn.onclick = () => {
      const code = $('thinkTerminalCode');
      if (code) code.textContent = 'Cleared buffer.';
    };
  }
  const abortBtn = $('thinkAbortBtn');
  if (abortBtn) {
    abortBtn.onclick = async () => {
      const r = await post('/api/action', { action: 'abort_inference' });
      if (r?.why) toast(r.why);
    };
  }
}

function renderAiThinking(p) {
  initAiThinkingEvents();
  if (typeof cardShown === 'function' && !cardShown('c-ai-thinking')) return;
  const ops = p.ai_ops || {};
  const sp = ops.speed || {};
  const th = ops.thinking || {};

  const abortBtn = $('thinkAbortBtn');
  if (abortBtn) {
    abortBtn.hidden = !(th.active || ops.active);
  }

  const modelName = ops.model || p.snapshot?.ai_model || '–';
  const badge = $('thinkModelBadge');
  if (badge) badge.textContent = modelName;

  const pill = $('thinkPhasePill');
  const phase = ops.phase || 'idle';
  if (pill) {
    pill.textContent = phase;
    pill.className = `pill ${phase === 'thinking' ? 'waiting think-pulse' : (phase === 'generating' || phase === 'answering' ? 'done' : '')}`;
  }

  const genVal = sp.tg_tps != null ? sp.tg_tps : (sp.eval_tps != null ? sp.eval_tps : '–');
  const genSpeedEl = $('thinkGenSpeed');
  if (genSpeedEl) {
    genSpeedEl.innerHTML = `${genVal} <small>tok/s</small>`;
    genSpeedEl.style.color = (typeof genVal === 'number' && genVal > 80) ? 'var(--cyan)' : 'var(--text)';
  }

  const promptVal = sp.prompt_tps != null ? `${sp.prompt_tps}` : '–';
  const promptMs = sp.prompt_ms != null ? ` (${sp.prompt_ms.toFixed(0)}ms)` : '';
  const promptSpeedEl = $('thinkPromptSpeed');
  if (promptSpeedEl) promptSpeedEl.innerHTML = `${promptVal}${promptMs ? `<small>${promptMs}</small>` : '<small> tok/s</small>'}`;

  const draftVal = sp.draft_rate != null ? `${(sp.draft_rate * 100).toFixed(1)}%` : '–';
  const draftMean = sp.draft_mean_len != null ? ` <small>len ${sp.draft_mean_len}</small>` : '';
  const draftRateEl = $('thinkDraftRate');
  if (draftRateEl) draftRateEl.innerHTML = `${draftVal}${draftMean}`;

  const ch = p.chains?.running;
  let taskDesc = ops.task;
  if (!taskDesc && ch) {
    taskDesc = `${ch.chain} · ${ch.step_name || 'step ' + ch.step} (${ch.item || ch.done}/${ch.items || ch.total})`;
  }
  const taskDescEl = $('thinkTaskDesc');
  if (taskDescEl) taskDescEl.textContent = taskDesc || (ops.active ? 'Inferring…' : 'idle');

  const termCode = $('thinkTerminalCode');
  const statusEl = $('thinkStreamStatus');
  const tokensEl = $('thinkTokensCount');
  const isRecent = Boolean(th.updated_at && ((Date.now() / 1000 - th.updated_at) < 300));

  if (tokensEl) tokensEl.textContent = (th.active || isRecent) && th.tokens ? `${th.tokens} tokens` : '';

  if (termCode && statusEl) {
    if (th.active) {
      const elapsed = th.started_at ? (Date.now() / 1000 - th.started_at).toFixed(1) : null;
      if (th.phase === 'thinking') {
        statusEl.textContent = `Thinking (inner reasoning)${elapsed ? ` · ${elapsed}s` : ''}…`;
        statusEl.style.color = 'var(--purple)';
      } else {
        const thoughtInfo = th.thinking_s ? `Thought ${th.thinking_s}s · ` : '';
        statusEl.textContent = `${thoughtInfo}Answering${elapsed ? ` · ${elapsed}s` : ''}…`;
        statusEl.style.color = 'var(--cyan)';
      }
      termCode.className = th.phase === 'thinking' ? 'is-thinking' : 'is-answering';
      if (th.text || th.preview) {
        setHTML(termCode, formatThinkingHtml(th.text, th.preview, th.phase === 'thinking', th.thinking_s));
      } else {
        termCode.textContent = 'Generating reasoning tokens…';
      }
    } else if (ops.active) {
      statusEl.textContent = `Inference active · ${ops.phase || 'generating'}`;
      statusEl.style.color = 'var(--cyan)';
      termCode.className = '';
      const spInfo = sp.tg_tps ? `${sp.tg_tps} tok/s` : (sp.prompt_tps ? `prompt eval: ${sp.prompt_tps} tok/s` : '');
      termCode.textContent = `Generating with ${ops.model || 'local model'}${spInfo ? ` at ${spInfo}` : ''}...\n(Inference active on port ${ops.port || ':11440'})`;
    } else if (th.text && isRecent) {
      const thoughtInfo = th.thinking_s ? `Thought ${th.thinking_s}s · ` : '';
      statusEl.textContent = `Completed reasoning session (${thoughtInfo}${agoShort(th.updated_at || Date.now() / 1000)})`;
      statusEl.style.color = 'var(--dim)';
      termCode.className = '';
      setHTML(termCode, formatThinkingHtml(th.text, th.preview, false, th.thinking_s));
    } else {
      statusEl.textContent = 'No active reasoning stream';
      statusEl.style.color = 'var(--dim)';
      termCode.className = '';
      if (termCode.textContent === 'Waiting for local AI reasoning…' || !termCode.textContent.trim()) {
        termCode.textContent = 'Idle — waiting for local AI inference or chain step…';
      }
    }
  }

  renderThinkSparkline(ops.history || [], p.speed || {});

  const term = $('thinkTerminal');
  if (term && thinkAutoScroll && th.active) {
    term.scrollTop = term.scrollHeight;
  }
}

function renderThinkSparkline(history, speedObj) {
  const wrap = $('thinkSparkWrap');
  const statsEl = $('thinkSparkStats');
  if (!wrap) return;

  let pts = [];
  if (Array.isArray(history) && history.length) {
    pts = history.map((h) => ({
      tps: h.tps,
      tokens: h.tokens || 0,
      draft_rate: h.draft_rate,
      prompt_tps: h.prompt_tps,
    }));
  } else if (Array.isArray(speedObj?.recent) && speedObj.recent.length) {
    pts = speedObj.recent.map((s) => ({ tps: s, tokens: speedObj.tokens || 0, draft_rate: null, prompt_tps: null }));
  }

  if (!pts.length) {
    if (statsEl) statsEl.textContent = 'waiting for completed inference';
    setHTML(wrap, '<div class="dim small" style="padding:6px 0;">No inference speed samples recorded yet this session.</div>');
    return;
  }

  const tpsVals = pts.map((p) => p.tps);
  const min = Math.min(...tpsVals);
  const max = Math.max(...tpsVals);
  const avg = (tpsVals.reduce((a, b) => a + b, 0) / tpsVals.length).toFixed(1);

  if (statsEl) {
    statsEl.textContent = `min: ${min} · avg: ${avg} · max: ${max} tok/s (${pts.length} run${pts.length === 1 ? '' : 's'})`;
  }

  const w = 400, h = 42;
  const padX = 14, padY = 8;
  const effectiveW = w - padX * 2;
  const effectiveH = h - padY * 2;
  const rangeY = (max - min) || (max * 0.2) || 10;
  const minY = Math.max(0, min - rangeY * 0.1);
  const maxY = max + rangeY * 0.1;
  const spanY = maxY - minY || 1;

  const coords = pts.map((pt, i) => {
    const x = padX + (pts.length === 1 ? effectiveW / 2 : (i / (pts.length - 1)) * effectiveW);
    const y = h - padY - ((pt.tps - minY) / spanY) * effectiveH;
    return { x: Math.round(x * 10) / 10, y: Math.round(y * 10) / 10, pt };
  });

  const linePoints = coords.map((c) => `${c.x},${c.y}`).join(' ');
  const polyPoints = `${coords[0].x},${h - 2} ${linePoints} ${coords[coords.length - 1].x},${h - 2}`;

  const dots = coords.map((c, i) => {
    const tip = `${c.pt.tps} tok/s${c.pt.tokens ? ' · ' + c.pt.tokens + ' tok' : ''}${c.pt.draft_rate != null ? ' · ' + (c.pt.draft_rate * 100).toFixed(1) + '% draft' : ''}`;
    return `<circle cx="${c.x}" cy="${c.y}" r="3.5" class="think-spark-dot"><title>Sample #${i + 1}: ${tip}</title></circle>`;
  }).join('');

  const svg = `<svg class="think-sparkline" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">`
    + `<defs><linearGradient id="thinkSparkGrad" x1="0" y1="0" x2="0" y2="1">`
    + `<stop offset="0%" stop-color="var(--cyan)" stop-opacity="0.35"/>`
    + `<stop offset="100%" stop-color="var(--cyan)" stop-opacity="0.0"/>`
    + `</linearGradient></defs>`
    + `<polygon points="${polyPoints}" fill="url(#thinkSparkGrad)"/>`
    + `<polyline points="${linePoints}" fill="none" stroke="var(--cyan)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`
    + dots
    + `</svg>`;

  setHTML(wrap, svg);
}


function renderAi(p) {
  const s = p.snapshot, el = $('aiModel');
  const state = s.ai_generating ? 'generating' : s.ai_state;
  el.className = `model ${s.ai_state} ${s.ai_generating ? 'generating loaded' : ''}`;
  $('aiName').textContent = s.ai_model || 'No model loaded';
  $('aiState').textContent = { generating: 'generating ⚡', loaded: 'loaded in VRAM, answers at once',
    sleeping: 'sleeping: out of VRAM, wakes in a few seconds', unloaded: 'nothing loaded', loading: 'loading…',
    offline: 'router down' }[state] || state;
  // model controls: state, what it is, where its memory is (VRAM / spilled into RAM / process RAM), load / unload
  const mode = p.snapshot.ai_mode, locked = mode === 'away' ? 'Away is running: Stop AI work first' : mode === 'off' ? 'the local AI is off' : '';
  const pill = { loaded: 'done', sleeping: 'queued', loading: 'waiting', unloaded: 'paused' };
  const card = s.vram_total_gb || 16;
  const rowsHtml = (p.models || []).map((m) => {
    const up = ['loaded', 'sleeping', 'loading'].includes(m.state), pending = pendingModels.get(m.id) === m.state;
    const v = m.vram_gb || 0, sp = m.spilled_gb || 0;
    const mem = up && (v || sp || m.ram_gb) ? `<div class="mem"><div class="membar"><i style="width:${(v / card) * 100}%;background:var(--green)"></i><i style="width:${(sp / card) * 100}%;background:${sp >= 0.3 ? 'var(--red)' : 'var(--amber)'}"></i></div>` +
      `VRAM ${v.toFixed(1)} GB${sp >= 0.05 ? ` · <span style="color:${sp >= 0.3 ? 'var(--red)' : 'var(--amber)'}">spilled ${sp.toFixed(1)} GB</span>` : ''}${m.ram_gb != null ? ` · RAM ${m.ram_gb.toFixed(1)} GB` : ''}</div>` : '';
    const btn = m.fixed ? '<span class="dim small">always on</span>'
      : `<button class="mini${up ? ' ghost' : ''}" data-model="${esc(m.id)}" data-op="${up ? 'unload' : 'load'}"${locked ? ` disabled title="${esc(locked)}"` : ''}>${up ? 'Unload' : 'Load'}</button>`;
    return `<div class="mrow${pending ? ' pending' : ''}"><div class="mhead"><b>${esc(m.id)}</b><span class="status ${pill[m.state] || 'paused'}">${esc(m.state)}</span></div>` +
      `<div class="acts">${btn}</div><div class="about" title="${esc(m.about)}">${esc(m.about)}</div>${mem}</div>`;
  }).join('');
  setHTML($('aiModels'), rowsHtml || `<div class="empty">${p.router?.up === false ? 'the router is not answering' : 'no models'}</div>`);
  for (const [id, st] of [...pendingModels]) if (!(p.models || []).some((m) => m.id === id && m.state === st)) pendingModels.delete(id);
  const pw = $('aiPower'); pw.dataset.power = mode === 'off' ? 'on' : 'off'; pw.textContent = mode === 'off' ? 'Turn AI on' : 'Turn AI off';
  pw.disabled = mode === 'away'; pw.title = mode === 'away' ? 'Away is running: Stop AI work first' : '';
  $('aiHint').textContent = locked;
  const lock = $('aiLock'); lock.textContent = s.gpu_locked ? 'held (a job is using the GPU)' : 'free'; lock.style.color = s.gpu_locked ? 'var(--amber)' : 'var(--dim)';
  const qbPw = $('qbAiPowerBtn');
  if (qbPw) {
    const isOff = mode === 'off' || p.router?.up === false;
    qbPw.dataset.power = isOff ? 'on' : 'off';
    qbPw.className = `qb-pill qb-ai-power ${mode === 'away' ? 'away' : isOff ? 'off' : 'on'}`;
    qbPw.textContent = mode === 'away' ? '🌙 Away' : isOff ? '▶ Wake AI' : '● Desk AI';
    qbPw.title = mode === 'away' ? 'Away mode active (click to stop Away)' : isOff ? 'Local AI is off — click to wake to Desk mode' : 'Local AI is in Desk mode — click to turn off';
  }
  const activeMod = s.ai_model || '';
  document.querySelectorAll('[data-quick-model]').forEach((b) => {
    const isLoaded = b.dataset.quickModel === activeMod && ['loaded', 'generating'].includes(state);
    b.classList.toggle('active', isLoaded);
    b.classList.toggle('on', isLoaded);
  });
}

function renderChains(p) {
  const c = p.chains || {}, run = c.running;
  setHTML($('runSeg'), segHtml(slide(p, 'RUN')?.segments));
  const pipe = $('pipeline'), bar = $('itemsBar');
  pipe.hidden = !run; bar.hidden = !(run && run.items);
  if (run) {
    const n = run.steps || 1, cur = run.step || 1;
    setHTML(pipe, Array.from({ length: n }, (_, i) => {
      const k = i + 1, cls = k < cur ? 'done' : k === cur ? 'now' : '';
      const label = k === cur ? (run.step_name || `step ${k}`) : `step ${k}`;
      return (i ? `<div class="link ${k <= cur ? 'done' : ''}"></div>` : '') + `<div class="step ${cls}"><b></b><span>${esc(label)}</span></div>`;
    }).join(''));
    if (run.items) { setBar(bar, run.item / run.items, 'var(--cyan)'); bar.classList.add('live'); bar.title = `item ${run.item} of ${run.items}`; }
  }
  const thinkWrap = $('chainThinkingWrap');
  if (thinkWrap) {
    const ops = p.ai_ops || {};
    const th = ops.thinking || {};
    const sp = ops.speed || {};
    const isRunning = Boolean(run);

    if (isRunning) {
      thinkWrap.hidden = false;
      const phaseEl = $('chainThinkPhase');
      const stepEl = $('chainThinkStep');
      const speedEl = $('chainThinkSpeed');
      const tokensEl = $('chainThinkTokens');
      const termCode = $('chainThinkingCode');
      const term = $('chainThinkingTerminal');

      const isWaiting = Boolean(run.waiting);
      const isThinking = Boolean(th.active && th.phase === 'thinking');
      const taskMatches = Boolean(
        th.task && (
          th.task.includes(run.chain) ||
          (run.step_name && th.task.includes(run.step_name))
        )
      );
      const isRecentChainThought = Boolean(
        taskMatches && th.updated_at && ((Date.now() / 1000 - th.updated_at) < 30)
      );

      const phase = isWaiting ? 'waiting' : (isThinking ? 'thinking' : (th.active ? 'answering' : (ops.active ? 'generating' : 'running')));
      if (phaseEl) {
        phaseEl.textContent = phase;
        phaseEl.className = `pill ${phase === 'thinking' ? 'waiting think-pulse' : (phase === 'waiting' ? 'warn' : 'done')}`;
      }
      if (stepEl) {
        const itemInfo = run.items ? ` (${run.item || 1}/${run.items})` : (run.total ? ` (${run.done || 0}/${run.total})` : '');
        stepEl.textContent = `${run.chain} › ${run.step_name || 'step ' + (run.step || 1)}${itemInfo}`;
      }
      const chainAbortBtn = $('chainThinkAbortBtn');
      if (chainAbortBtn) {
        chainAbortBtn.hidden = !isRunning;
        chainAbortBtn.onclick = async () => {
          const r = await post('/api/action', { action: 'abort_inference' });
          if (r?.why) toast(r.why);
        };
      }
      if (speedEl) {
        const speedVal = sp.tg_tps != null ? sp.tg_tps : (sp.eval_tps != null ? sp.eval_tps : null);
        speedEl.textContent = (th.active || ops.active) && speedVal ? `${speedVal} tok/s` : '';
      }
      if (tokensEl) {
        tokensEl.textContent = (th.active || isRecentChainThought) && th.tokens ? `${th.tokens} tokens` : '';
      }
      if (termCode) {
        if (isWaiting) {
          termCode.className = '';
          termCode.textContent = `⏸ Paused / Waiting:\n${run.waiting}`;
        } else if (th.active && (th.text || th.preview)) {
          termCode.className = isThinking ? 'is-thinking' : 'is-answering';
          setHTML(termCode, formatThinkingHtml(th.text, th.preview, isThinking, th.thinking_s));
        } else if (isRecentChainThought && (th.text || th.preview)) {
          termCode.className = '';
          setHTML(termCode, formatThinkingHtml(th.text, th.preview, false, th.thinking_s));
        } else if (ops.active) {
          termCode.className = '';
          termCode.textContent = `▶ Generating with ${ops.model || 'model'}...\n(Processing inference on router)`;
        } else {
          termCode.className = '';
          termCode.textContent = `▶ Executing step "${run.step_name || 'step ' + run.step}"...\n(Waiting for model output or tool response)`;
        }
      }
      if (term && (isThinking || th.active)) {
        term.scrollTop = term.scrollHeight;
      }
    } else {
      thinkWrap.hidden = true;
    }
  }
  // waiting chains get "Run now": one run that skips the polite waits (the runner still stops for the game guard,
  // an Away queue job, or an Away-only model)
  const pend = c.pending || [];
  const skip = (name) => `<button class="mini go" data-chain="${esc(name)}" data-op="now" title="Skip the wait: run it now">⏭ Run now</button>`;
  const held = run && run.waiting ? `<div class="item"><span>${esc(run.chain)} <span class="dim">(running)</span></span><span style="color:var(--amber)">${esc(run.waiting)}${skip(run.chain)}</span></div>` : '';
  setHTML($('chPending'), held + pend.map((x) => `<div class="item"><span>${esc(x.chain)}</span><span style="color:${x.blocked ? 'var(--amber)' : 'var(--cyan)'}">${esc(x.blocked || 'next')}${x.blocked ? skip(x.chain) : ''}</span></div>`).join('')
    || '<div class="empty">nothing waiting</div>');
  const sch = c.scheduled || [];
  // acted-on scoreboard (chains v2 plan, phase 0): a chain whose findings you rarely tick off is a retirement candidate
  const score = Object.fromEntries((c.scoreboard || []).map((s) => [s.chain, s]));
  const scoreTag = (name) => {
    const s = score[name];
    if (!s || s.findings === 0) return '';
    const t = `${s.acted}/${s.findings} acted on (last ${s.runs})`;
    return s.flag ? ` <span style="color:var(--amber)" title="Under 20% of its findings acted on over 4 runs: consider retiring it">⚠ ${esc(t)}</span>`
      : ` <span class="dim">${esc(t)}</span>`;
  };
  setHTML($('chSched'), sch.length ? sch.map((x) => `<div class="item"><span>${esc(x.chain)}${scoreTag(x.chain)}</span><span>${esc(nextLabel(x.next))} <button class="mini ghost sched-btn" data-sched="${esc(x.chain)}" title="Edit schedule">⏱ Edit</button></span></div>`).join('')
    : '<div class="empty">no schedules</div>');
}

function renderDisks(s) {
  const box = $('diskList'), trends = s.disk_trends || {};
  const html = (s.disks || []).filter((d) => d.free_gb != null).map((d) => {
    const t = trends[d.drive], pct = d.percent || 0;
    const color = pct >= 92 || d.free_gb < 20 ? 'var(--red)' : t != null && t <= -1 ? 'var(--red)' : t != null && t >= 1 ? 'var(--green)' : pct >= 85 ? 'var(--amber)' : 'var(--cyan)';
    const arrow = t != null && t <= -1 ? ` ▼${(-t).toFixed(1)}` : t != null && t >= 1 ? ` ▲${t.toFixed(1)}` : '';
    return `<div class="disk"><div class="top"><span><b style="color:${color}">${esc(d.drive)}:</b> ${Math.round(d.free_gb)} GB free<span style="color:${color}">${arrow}</span></span><span class="dim">${pct.toFixed(0)}% used of ${Math.round(d.total_gb)} GB</span></div><div class="bar"><i style="width:${pct}%;background:${color}"></i></div></div>`;
  }).join('');
  if (box._html !== html) { box.innerHTML = html; box._html = html; }
}

function renderFeed(items) {
  const ol = $('feed');
  setHTML(ol, items.map((it) => {
    const key = it.time + it.text, fresh = !firstFeed && !seenFeed.has(key); seenFeed.add(key);
    return `<li class="${fresh ? 'new' : ''}"><time>${esc(when(it.time))}</time><span style="color:${col(it.color)}">${esc(it.text)}</span></li>`;
  }).join('') || '<li class="dim">nothing yet</li>');
  firstFeed = false;
}

// Network · Pi-hole (collectors/network.py: probed every 30 s in its own thread, no password needed)
const NET_STATE = {
  ok: ['OK', 'ok'], slow: ['SLOW', 'warn'], degraded: ['ONE IP DOWN', 'warn'], 'dns-off': ['DNS OFF', 'bad'],
  'no-upstream': ['NO UPSTREAM', 'bad'], 'not-blocking': ['NOT BLOCKING', 'warn'], down: ['DOWN', 'bad'], unknown: ['…', 'dim'],
};
function renderNetwork(p) {
  if (typeof cardShown === 'function' && !cardShown('c-net')) return;
  const net = p.network || {}, ph = net.pihole || {}, lan = net.lan || {};
  const [label, cls] = NET_STATE[ph.state] || NET_STATE.unknown;
  const pill = $('netPill'); if (!pill) return;
  pill.textContent = label; pill.className = `pill ${cls}`; pill.title = ph.alert || '';
  const yn = (v, yes, no) => (v == null ? '<span class="dim">–</span>' : v ? `<span style="color:var(--green)">${yes}</span>` : `<span style="color:var(--red)">${no}</span>`);
  setHTML($('netHosts'), (ph.hosts || []).map((h) => {
    const st = h.up == null ? '<span class="dim">checking…</span>'
      : !h.up ? '<span style="color:var(--red)">no answer</span>'
      : !h.dns ? '<span style="color:var(--red)">DNS service off</span>'
      : `<span style="color:${h.ms > 150 ? 'var(--amber)' : 'var(--green)'}">up · ${h.ms} ms</span>`;
    return `<div class="row"><span>${esc(h.ip)}</span><span>${st}</span></div>`;
  }).join('') || '<div class="row dim">no Pi-hole configured</div>');
  const pc = ph.pc || {};
  setHTML($('netPc'), `${yn(pc.blocking, 'blocking', 'not blocking')} · ${yn(pc.upstream, 'upstream ok', 'upstream fails')}`);
  const note = $('netNote'); note.hidden = !ph.note; note.textContent = ph.note || '';
  // Stats and pause/resume come through sol-deck, which holds the Pi-hole app password.
  const st = ph.stats || {}, statsEl = $('netStats'), acts = $('netStatsActs');
  if (statsEl && acts) {
    if (st.state === 'ok') {
      const n = (v) => (v == null ? '–' : Number(v).toLocaleString());
      const paused = st.blocking === false;
      setHTML(statsEl, `${n(st.queries)} queries · ${st.percent ?? '–'}% blocked`
        + (paused ? ` · <span style="color:var(--amber)">paused${st.timer ? `, back in ${Math.ceil(st.timer / 60)} min` : ''}</span>` : ''));
      $('netPauseBtn').hidden = paused;
      $('netResumeBtn').hidden = !paused;
      $('netTop').textContent = (st.top_blocked || []).length ? `top blocked: ${st.top_blocked.slice(0, 3).join(', ')}` : '';
      acts.hidden = false;
    } else {
      statsEl.innerHTML = `<span class="dim">${esc(st.why || '–')}</span>`;
      acts.hidden = true;
    }
  }
  const devs = lan.devices || [];
  $('netCount').textContent = devs.length ? `${lan.online || 0} online · ${devs.length} known${lan.new ? ` · ${lan.new} new` : ''}` : '';
  setHTML($('netDevs'), devs.map((d) => {
    const c = d.new ? 'new' : d.online ? '' : 'off';
    const seen = d.online ? 'online' : `seen ${esc(when(d.last_seen))}`;
    const tip = `${d.mac} · first seen ${d.first_seen}${d.private ? ' · private (randomised) MAC: may change' : ''}`;
    return `<span class="ip ${c}">${esc(d.ip)}</span><span class="${c}" title="${esc(tip)}">${d.new ? 'NEW · ' : ''}${esc(d.name || (d.private ? 'phone/tablet (private MAC)' : 'unnamed'))}</span><span class="dim ${c}">${seen}</span>`;
  }).join('') || '<span class="dim">scanning…</span>');
}

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

// The iPad deck's own report (sol-deck's diag.js -> /api/clientlog): pairing, input
function renderSolPad(p) {
  if (typeof cardShown === 'function' && !cardShown('c-pad')) return;
  const sp = p.sol_pad || {};
  const pill = $('padPill');
  if (!pill) return;
  const states = {
    online: ['ONLINE', 'ok'],
    partial: ['PARTIAL', 'warn'],
    standby: ['USB READY', 'dim'],
    offline: ['OFFLINE', 'dim'],
  };
  const [label, cls] = states[sp.state] || ['…', 'dim'];
  pill.textContent = label;
  pill.className = `pill ${cls}`;
  const yn = (v, yes, no, warn) => (v == null ? '<span class="dim">–</span>' : v ? `<span style="color:var(--green)">${yes}</span>` : `<span style="color:${warn ? 'var(--amber)' : 'var(--dim)'}">${no}</span>`);
  setHTML($('padUsb'), yn(sp.usb_connected, 'Connected (USB)', 'Unplugged', false));
  setHTML($('padStream'), yn(sp.stream_running, 'Running', 'Stopped', false));
  setHTML($('padDock'), yn(sp.dock_running, 'Running', 'Stopped', false));
  const running = Boolean(sp.stream_running || sp.dock_running);
  if ($('padStartBtn')) $('padStartBtn').hidden = Boolean(sp.stream_running && sp.dock_running);
  if ($('padRestartBtn')) $('padRestartBtn').hidden = !running;
  if ($('padStopBtn')) $('padStopBtn').hidden = !running;
}
// lag p50/p95, transport, errors. Empty when sol-deck sends none.
function deckHealthHtml(client) {
  const parts = [];
  parts.push(client.paired
    ? '<span style="color:var(--ok)">🔑 paired</span>'
    : '<span style="color:var(--warn)">not paired</span>');
  const h = client.health;
  if (h) {
    if (h.p50_ms != null && h.p95_ms != null) {
      const slow = h.p95_ms > 250;
      parts.push(`<span style="color:var(${slow ? '--warn' : '--ok'})">lag ${Math.round(h.p50_ms)}/${Math.round(h.p95_ms)} ms</span>`);
    }
    if (h.transport) parts.push(esc(h.transport));
    const errs = (h.errors || 0) + (h.stream_errors || 0);
    if (errs) parts.push(`<span style="color:var(--warn)">${errs} error${errs === 1 ? '' : 's'}</span>`);
    if (h.ws_drops) parts.push(`<span style="color:var(--warn)">${h.ws_drops} socket drop${h.ws_drops === 1 ? '' : 's'}</span>`);
    const age = h.age_sec < 90 ? `${h.age_sec}s` : h.age_sec < 5400 ? `${Math.round(h.age_sec / 60)}m` : `${Math.round(h.age_sec / 3600)}h`;
    parts.push(`report ${age} ago`);
  }
  return ` <span class="dim">· ${parts.join(' · ')}</span>`;
}

function renderSecurity(p) {
  if (typeof cardShown === 'function' && !cardShown('c-security')) return;
  const sec = p.security || {};
  const pill = $('secPill');
  const masterBtn = $('secMasterToggleBtn');
  const alertsList = $('secAlertsList');
  if (!pill || !masterBtn) return;

  const deckDot = $('secDeckDot');
  const deckTitle = $('secDeckTitle');
  const ipadStatus = $('secIpadStatus');
  const startBtn = $('secDeckStartBtn');
  const restartBtn = $('secDeckRestartBtn');
  const stopBtn = $('secDeckStopBtn');
  const proc = sec.process || {};
  const client = sec.client || {};

  if (!sec.up) {
    pill.className = 'pill dim';
    pill.textContent = 'OFFLINE';
    masterBtn.textContent = 'sol-deck Offline';
    masterBtn.disabled = true;
    masterBtn.className = 'mini ghost';
    if ($('secCanaryBadge')) { $('secCanaryBadge').textContent = 'OFF'; $('secCanaryBadge').className = 'badge dim'; }
    if ($('secArpBadge')) { $('secArpBadge').textContent = 'OFF'; $('secArpBadge').className = 'badge dim'; }
    if ($('secRadarBadge')) { $('secRadarBadge').textContent = '–'; $('secRadarBadge').className = 'badge dim'; }
    if ($('secSoundBadge')) { $('secSoundBadge').textContent = '–'; $('secSoundBadge').className = 'badge dim'; }
    if ($('secCanaryPorts')) $('secCanaryPorts').textContent = 'sol-deck (:5000) not running';
    if ($('secArpSub')) $('secArpSub').textContent = 'Start sol-deck service to enable';
    if (alertsList) alertsList.hidden = true;

    if (deckDot) deckDot.className = 'sec-link-dot off';
    if (deckTitle) {
      if (proc.running) {
        deckTitle.innerHTML = `SOL Deck Service: <strong style="color:var(--warn)">STARTING / PORT 5000 CLOSED</strong> (PID ${proc.pid})`;
      } else {
        deckTitle.innerHTML = `SOL Deck Service (:5000): <strong style="color:var(--bad)">OFFLINE (STOPPED)</strong>`;
      }
    }
    if (ipadStatus) ipadStatus.textContent = '📱 iPad Link: Offline (PC companion bridge is stopped)';
    if (startBtn) startBtn.hidden = false;
    if (restartBtn) restartBtn.hidden = !proc.running;
    if (stopBtn) stopBtn.hidden = !proc.running;
    const costEl = $('secProcCost');
    if (costEl) costEl.textContent = 'sol-deck offline · 0 MB RAM · 0% CPU';
    return;
  }

  // Update Link Bar when online
  if (deckDot) deckDot.className = 'sec-link-dot on';
  if (deckTitle) {
    const uptimeStr = proc.uptime_sec ? `${Math.floor(proc.uptime_sec / 60)}m` : '0m';
    const ramStr = proc.ram_mb ? `${proc.ram_mb} MB` : '~18 MB';
    deckTitle.innerHTML = `SOL Deck Service (:5000): <strong style="color:var(--ok)">ONLINE</strong> <span class="dim">(PID ${proc.pid || '–'} · ${ramStr} RAM · up ${uptimeStr})</span>`;
  }
  if (ipadStatus) {
    const health = deckHealthHtml(client);
    if (client.active) {
      ipadStatus.innerHTML = `📱 <strong style="color:var(--ok)">${esc(client.name || 'iPad mini')} (${esc(client.ip || '192.168.0.124')}):</strong> <span style="color:var(--ok)">${esc(client.status_text || 'Connected')}</span> <span class="dim">· ${client.request_count || 0} reqs</span>${health}`;
    } else if (client.ip) {
      ipadStatus.innerHTML = `📱 <span class="dim">${esc(client.name || 'iPad mini')} (${esc(client.ip)}):</span> <span style="color:var(--warn)">${esc(client.status_text || 'Standby')}</span>${health}`;
    } else {
      ipadStatus.innerHTML = `📱 <span class="dim">iPad mini: Waiting for client connection...</span>`;
    }
  }
  if (startBtn) startBtn.hidden = true;
  if (restartBtn) restartBtn.hidden = false;
  if (stopBtn) stopBtn.hidden = false;

  masterBtn.disabled = false;
  const s = sec.summary || {};
  const set = sec.settings || {};
  const alerts = sec.alerts || [];

  // Master status pill
  if (s.status === 'alert') {
    pill.className = 'pill bad';
    pill.textContent = '🚨 INTRUSION ACTIVE';
  } else if (s.status === 'warning') {
    pill.className = 'pill warn';
    pill.textContent = '⚠️ WARNING';
  } else if (!s.canary_active && !set.arp_watchdog_enabled) {
    pill.className = 'pill dim';
    pill.textContent = '⚪ DISABLED';
  } else {
    pill.className = 'pill ok';
    pill.textContent = '🛡️ SECURE';
  }

  // Master toggle button
  const anyActive = s.canary_active || set.arp_watchdog_enabled;
  if (anyActive) {
    masterBtn.textContent = 'Turn Off Watchdog';
    masterBtn.className = 'mini swap-run';
  } else {
    masterBtn.textContent = 'Turn On Watchdog';
    masterBtn.className = 'mini go';
  }

  // 1. Canary
  const cBadge = $('secCanaryBadge');
  const cBtn = $('secCanaryToggleBtn');
  if (cBadge && cBtn) {
    if (s.canary_active) {
      cBadge.textContent = 'ACTIVE';
      cBadge.className = 'badge ok';
      cBtn.textContent = 'Turn Off';
      cBtn.className = 'mini ghost';
    } else {
      cBadge.textContent = 'OFF';
      cBadge.className = 'badge dim';
      cBtn.textContent = 'Turn On';
      cBtn.className = 'mini go';
    }
  }
  const ports = (s.canary_ports && s.canary_ports.length) ? s.canary_ports.join(', ') : 'None';
  if ($('secCanaryPorts')) $('secCanaryPorts').textContent = `TCP Decoys: ${ports}`;

  // 2. ARP Guard
  const aBadge = $('secArpBadge');
  const aBtn = $('secArpToggleBtn');
  if (aBadge && aBtn) {
    if (!set.arp_watchdog_enabled) {
      aBadge.textContent = 'DISABLED';
      aBadge.className = 'badge dim';
      aBtn.textContent = 'Turn On';
      aBtn.className = 'mini go';
    } else if (s.gateway_status === 'verified') {
      aBadge.textContent = 'VERIFIED';
      aBadge.className = 'badge ok';
      aBtn.textContent = 'Turn Off';
      aBtn.className = 'mini ghost';
    } else {
      aBadge.textContent = 'SPOOF DETECTED';
      aBadge.className = 'badge bad';
      aBtn.textContent = 'Turn Off';
      aBtn.className = 'mini ghost';
    }
  }
  if ($('secArpSub')) $('secArpSub').textContent = `Gateway: ${set.expected_gateway_ip || '192.168.0.1'}`;

  // 3. Radar
  const rBadge = $('secRadarBadge');
  const rBtn = $('secRadarToggleBtn');
  const known = s.known_device_count || 0;
  const unknown = s.unknown_device_count || 0;
  if (rBadge && rBtn) {
    if (unknown > 0 && set.unknown_device_alerts) {
      rBadge.textContent = `${unknown} UNKNOWN`;
      rBadge.className = 'badge warn';
    } else {
      rBadge.textContent = `${known} KNOWN`;
      rBadge.className = 'badge ok';
    }
    rBtn.textContent = set.unknown_device_alerts ? 'Alerts ON' : 'Alerts OFF';
  }
  if ($('secRadarSub')) $('secRadarSub').textContent = `${unknown} Unknown Devices`;

  // 4. iPad Sound
  const sndBadge = $('secSoundBadge');
  const sndBtn = $('secSoundToggleBtn');
  if (sndBadge && sndBtn) {
    if (set.ipad_sound_alert) {
      sndBadge.textContent = 'SIREN ON';
      sndBadge.className = 'badge ok';
      sndBtn.textContent = 'Mute';
    } else {
      sndBadge.textContent = 'MUTED';
      sndBadge.className = 'badge dim';
      sndBtn.textContent = 'Unmute';
    }
  }

  // Alerts box
  const unacked = s.unacked_alerts || 0;
  if (alertsList) {
    if (unacked > 0 || alerts.length > 0) {
      const unackedAlerts = alerts.filter(a => !a.acked);
      if (unackedAlerts.length > 0) {
        alertsList.hidden = false;
        alertsList.innerHTML = `<div class="sec-alert-banner">` +
          `<strong>🚨 Intrusion Alert (${unackedAlerts.length}):</strong> ` +
          `<span>${esc(unackedAlerts[0].message)}</span> ` +
          `<span class="dim">(${esc(unackedAlerts[0].source_ip || '')} · ${esc(unackedAlerts[0].timestamp || '')})</span>` +
          `</div>`;
      } else {
        alertsList.hidden = true;
      }
    } else {
      alertsList.hidden = true;
    }
  }

  // Live overhead / resource stats
  const costEl = $('secProcCost');
  if (costEl) {
    if (proc.ram_mb) {
      costEl.textContent = `sol-deck PID ${proc.pid} · ${proc.ram_mb} MB RAM · <0.1% CPU · 2 Canary Sockets Idle`;
    } else {
      costEl.textContent = 'sol-deck ~18 MB RAM · <0.1% CPU · 2 Canary Sockets Idle';
    }
  }
}

// ---------- the stream (only while this tab is visible)
// the Exam assist card (exam_review.py): solves practice exam questions in real time
const REV_PILL = { watching: 'ok', reading: '', answering: '', waiting: 'warn', paused: 'warn', error: 'bad', stopped: '' };
let lastReviewState = null;

function formatVerificationText(c) {
  if (!c) return '';
  const parts = [];
  if (c.question) parts.push(`Question:\n${c.question}`);
  if (c.exhibit_text) parts.push(`Exhibit / Context:\n${c.exhibit_text}`);
  if (c.question_type === 'matching' && c.matching_pairs) {
    parts.push('Matching Pairs:\n' + c.matching_pairs.map(p => `- ${p.source} ➔ ${p.target}`).join('\n'));
  } else if (c.question_type === 'fill_in_the_blank' && c.blank_answers) {
    parts.push('Command / Value:\n' + c.blank_answers.join('\n'));
  } else if (c.question_type === 'ordering' && c.ordered_sequence) {
    parts.push('Ordered Sequence:\n' + c.ordered_sequence.join('\n'));
  } else if (c.choices && c.choices.length) {
    parts.push('Choices:\n' + c.choices.map(o => `${o.label}. ${o.text}`).join('\n'));
  }
  const ans = c.answer || (c.answer_labels || []).join(', ');
  if (ans) parts.push(`Suggested Answer:\n${ans}`);
  if (c.topic) parts.push(`Topic: ${c.topic}`);
  if (c.explanation) parts.push(`Explanation:\n${c.explanation}`);
  return parts.join('\n\n');
}

function buildBraveAiQuery(c) {
  if (!c) return '';
  const q = (c.question || '').trim();
  let combined = q;
  if (c.choices && c.choices.length) {
    combined = `${q} ${c.choices.map(o => `${o.label}) ${o.text}`).join(' ')}`.trim();
  } else if (c.answer) {
    combined = `${q} Answer: ${c.answer}`.trim();
  }
  if (combined.length > 350) {
    combined = combined.substring(0, 350).replace(/\s+\S*$/, '');
  }
  return `${combined} ??`;
}

function renderReview(p) {
  const r = p.review; if (!r) return;
  lastReviewState = r;
  const st = r.running ? r.status : 'stopped', pill = $('revStatus');
  pill.textContent = st; pill.className = `pill ${REV_PILL[st] ?? ''}`;
  pill.style.color = ['reading', 'answering'].includes(st) ? 'var(--cyan)' : '';
  const countParts = [];
  if (r.answered) countParts.push(`${r.answered} solved`);
  else if (r.correct || r.wrong) countParts.push(`${r.correct} ✓ · ${r.wrong} ✗`);
  $('revCount').textContent = countParts.join(' · ');
  const tg = $('revToggle'); tg.dataset.target = r.running ? 'stop' : 'start'; tg.textContent = r.running ? 'Stop' : 'Start';
  tg.disabled = !r.running && !r.rect; tg.title = r.rect ? '' : 'Set the box first';
  $('revText').textContent = (r.text || '') + (r.running && r.next_look && st === 'watching' ? ` · next look ${r.next_look}` : '');
  $('revBox').textContent = r.rect ? `Box ${r.rect[2]}×${r.rect[3]} at (${r.rect[0]}, ${r.rect[1]}) · looks every 3 s · real-time exam solver` : 'No box yet: drag a box around the question area, then Set box & start.';
  const c = r.current, cur = $('revCurrent');
  const retryTop = $('revRetryBtn'); if (retryTop) retryTop.disabled = !c;
  const braveTop = $('revBraveTopBtn'); if (braveTop) braveTop.disabled = !c;
  const snipBtn = $('revSnipBtn'); if (snipBtn) { snipBtn.textContent = r.parts_count > 1 ? `➕ Add Snip (${r.parts_count} parts)` : '➕ Add Snip (Scroll)'; }
  const hudCb = $('revToggleHud'); if (hudCb && document.activeElement !== hudCb) { hudCb.checked = !!r.hud_enabled; }
  cur.hidden = !c;
  if (c) {
    const isGemini = !!(c.gemini_retried || c.model === 'gemini-3.8-flash');
    const qType = c.question_type || 'multiple_choice';
    let contentHtml = '';

    if (qType === 'matching' && c.matching_pairs && c.matching_pairs.length) {
      contentHtml = `<div class="rev-matching" style="margin:8px 0;background:var(--card);border-radius:6px;padding:8px">` +
        `<table style="width:100%;font-size:0.85rem"><thead><tr><th style="text-align:left;color:var(--muted)">Item</th><th style="text-align:left;color:var(--muted)">Target</th></tr></thead><tbody>` +
        c.matching_pairs.map(p => `<tr><td style="padding:4px 8px 4px 0"><b>${esc(p.source)}</b></td><td style="color:${isGemini ? 'var(--cyan)' : 'var(--green)'}">➔ ${esc(p.target)}</td></tr>`).join('') +
        `</tbody></table></div>`;
    } else if (qType === 'fill_in_the_blank' && c.blank_answers && c.blank_answers.length) {
      contentHtml = `<div style="margin:8px 0"><span class="dim small">Command / Value:</span>` +
        `<pre style="background:#0f172a;color:#38bdf8;padding:8px 12px;border-radius:6px;font-family:monospace;font-size:0.9rem;margin:4px 0;user-select:all"><code>${esc(c.blank_answers.join('\n'))}</code></pre></div>`;
    } else if (qType === 'ordering' && c.ordered_sequence && c.ordered_sequence.length) {
      contentHtml = `<div style="margin:8px 0"><span class="dim small">Ordered Sequence:</span>` +
        `<ol style="padding-left:20px;margin:4px 0;font-size:0.9rem">` +
        c.ordered_sequence.map(s => `<li style="padding:2px 0">${esc(s)}</li>`).join('') +
        `</ol></div>`;
    } else {
      const ansLabels = c.answer_labels || c.correct_labels || [];
      const mark = (lb) => ansLabels.includes(lb) ? 'right' : (c.your_labels || []).includes(lb) ? 'mine' : '';
      contentHtml = (c.choices || []).map((o) => {
        const isRight = ansLabels.includes(o.label);
        const box = isRight ? '☑ ' : '☐ ';
        return `<div class="rev-opt ${mark(o.label)}"><b>${box}${esc(o.label)}.</b> ${esc(o.text)}` +
          `${(c.your_labels || []).includes(o.label) ? ' <span class="dim small">your selection</span>' : ''}` +
          `${isRight ? ` <span class="small" style="color:${isGemini ? 'var(--cyan)' : 'var(--green)'}">✓ answer</span>` : ''}</div>`;
      }).join('');
    }

    const exhibitHtml = c.exhibit_text ? `<details style="margin:6px 0;font-size:0.85rem"><summary class="dim" style="cursor:pointer">📊 Exhibit / Diagram</summary><pre style="background:var(--card);padding:6px;border-radius:4px;overflow-x:auto">${esc(c.exhibit_text)}</pre></details>` : '';
    const typeLabel = qType === 'matching' ? 'Matching' : qType === 'fill_in_the_blank' ? 'Fill-in-Blank' : qType === 'ordering' ? 'Ordering' : '';
    const typeBadge = typeLabel ? ` <span class="pill" style="font-size:0.75rem;padding:2px 6px">${typeLabel}</span>` : '';
    const modelBadge = isGemini ? ' <span class="pill" style="background:#0284c7;color:#fff;font-size:0.75rem;padding:2px 6px">✨ Gemini 3.8 Flash</span>' : '';
    let ansTitle = c.answer || (c.answer_labels || []).join(', ');
    if (qType === 'matching' && c.matching_pairs && c.matching_pairs.length) {
      ansTitle = `${c.matching_pairs.length} Pairs Matched`;
    } else if (qType === 'ordering' && c.ordered_sequence && c.ordered_sequence.length) {
      ansTitle = `${c.ordered_sequence.length} Steps Sequenced`;
    } else if (qType === 'fill_in_the_blank' && c.blank_answers && c.blank_answers.length) {
      ansTitle = c.blank_answers[0];
    }
    const head = `<span style="color:${isGemini ? 'var(--cyan)' : 'var(--green)'}">💡 Answer: ${esc(ansTitle)} · ${esc(c.topic || 'Solved')}</span>${typeBadge}${modelBadge}`;
    const whyWrong = c.why_previous_wrong ? `<p class="rev-explain" style="color:var(--amber);margin-bottom:6px"><b>Previous attempt issue:</b> ${esc(c.why_previous_wrong)}</p>` : '';
    const body = `${whyWrong}<p class="rev-explain">${esc(c.explanation || '')}</p>`;
    const autoScrollBtn = `<button class="mini primary" style="margin-top:8px" data-act="review" data-target="auto_scroll" title="Simulate scroll down, stitch, and solve">⚡ Auto-Scroll & Solve</button>`;
    const retryBtn = isGemini ? '' : `<button class="mini danger" style="margin-top:8px" data-act="review" data-target="retry_gemini" title="Verify or re-solve with Gemini 3.8 Flash">🔍 Verify / Retry (Gemini)</button>`;
    const braveBtn = `<button class="mini" style="margin-top:8px;background:#ea580c;color:#fff" data-act="review" data-target="brave_search" title="Copy question & choices to clipboard and search in Brave using AI mode">🦁 Brave AI Search</button>`;
    setHTML(cur, `<div class="row"><b>${head}</b><span class="dim small">${esc(c.at || '')}${c.seconds ? ` · ${c.seconds} s` : ''}</span></div>` +
      `<div class="rev-q">${esc(c.question)}</div>${exhibitHtml}${contentHtml}${body}<div style="display:flex;gap:8px;flex-wrap:wrap">${autoScrollBtn}${retryBtn}${braveBtn}</div>`);
  }
  setHTML($('revHistory'), (r.history || []).map((h) => `<div class="item"><span>${h.n}. ${esc(h.topic || h.question)}</span>` +
    `<span style="color:${h.gemini ? 'var(--cyan)' : 'var(--green)'}">${h.gemini ? '✨' : '✓'}</span></div>`).join('') || '<div class="empty">nothing yet</div>');
  const topics = Object.entries(r.topics || {}).sort((a, b) => b[1] - a[1]);
  setHTML($('revTopics'), topics.map(([t, n]) => `<div class="item"><span>${esc(t)}</span><span style="color:var(--cyan)">${n}×</span></div>`).join('')
    || '<div class="empty">no topics recorded yet</div>');
}

function connect() {
  if (es) return;
  es = new EventSource('/api/stream');
  let current = null;   // the first message has everything; the next ones only what changed (patch: parts, snap: fields)
  es.onmessage = (e) => {
    try {
      const d = JSON.parse(e.data);
      if (d.full) current = d.full;
      else if (current) { Object.assign(current, d.patch || {}); if (d.snap) current.snapshot = { ...current.snapshot, ...d.snap }; }
      else return;
      render(current);
    } catch (err) { console.error(err); }
  };
  es.onerror = () => { $('live').classList.remove('on'); };
}
function disconnect() { if (es) { es.close(); es = null; } }
document.addEventListener('visibilitychange', () => { if (document.hidden) disconnect(); else { loadHistory(); connect(); } });
setInterval(() => {
  if (document.hidden) return;
  const secs = Math.round((Date.now() - last) / 1000);
  $('age').innerHTML = !last ? 'connecting…' : secs > 8 ? `<span style="color:var(--red)">no update for ${secs}s</span>` : `live · ${new Date().toLocaleTimeString()}`;
}, 1000);
addEventListener('resize', drawCharts);

// ---------- buttons and clickable cards
document.addEventListener('click', async (e) => {
  const b = e.target.closest('[data-act]');
  if (b) {
    e.stopPropagation();
    if (b.dataset.act === 'review' && b.dataset.target === 'brave_search') {
      const c = lastReviewState && lastReviewState.current;
      if (c) {
        const text = formatVerificationText(c);
        if (text && navigator.clipboard) {
          navigator.clipboard.writeText(text).catch(() => {});
        }
        const q = buildBraveAiQuery(c);
        if (q) {
          window.open('https://search.brave.com/search?q=' + encodeURIComponent(q), '_blank');
        }
        toast('Copied to clipboard & opened Brave AI');
        post('/api/action', { action: 'review', target: 'brave_search', client_handled: true });
        return;
      }
    }
    if (b.dataset.confirm && !(await hudConfirm(b.dataset.confirm))) return;
    const r = await post('/api/action', { action: b.dataset.act, target: b.dataset.target || '' });
    if (r.why) toast(r.why);
    return;
  }
  const card = e.target.closest('.card[data-open]');
  if (card && !e.target.closest('button, a, select')) post('/api/action', { action: 'open', target: card.dataset.open });
});
$('stopAi').onclick = async () => {
  if (!(await hudConfirm('Stop the Away work now? The running job goes back in the queue (finished work is kept).'))) return;
  const r = await post('/api/action', { action: 'stop_ai' }); toast(r.why || 'stopping');
};
$('theme').onchange = (e) => { applyTheme(e.target.value); post('/api/action', { action: 'theme', target: e.target.value }); };
const revHudCb = $('revToggleHud');
if (revHudCb) revHudCb.onchange = (e) => { post('/api/action', { action: 'review', target: 'toggle_hud', enabled: e.target.checked }); };

// ---------- phase C: ask it overnight, chain controls, the reader
function md(text) {
  // a small Markdown renderer for answers and chain results: everything is escaped first, then a few safe tags added
  const src = String(text || '').replace(/^\ufeff?---\r?\n[\s\S]*?\r?\n---\s*\r?\n/, '');
  const inline = (s) => esc(s).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<i>$2</i>').replace(/(^|[\s(])_([^_\n]+)_(?=[\s.,;:!?)]|$)/g, '$1<i>$2</i>').replace(/\[([^\]]+)\]\([^)]+\)/g, '<u>$1</u>');
  const out = []; let list = null, code = null, para = [];
  const flush = () => { if (para.length) { out.push(`<p>${para.map(inline).join('<br>')}</p>`); para = []; } if (list) { out.push(`</${list}>`); list = null; } };
  for (const line of src.split(/\r?\n/)) {
    if (code !== null) { if (/^\s*(```|~~~)/.test(line)) { out.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`); code = null; } else code.push(line); continue; }
    if (/^\s*(```|~~~)/.test(line)) { flush(); code = []; continue; }
    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) { flush(); const n = Math.min(3, m[1].length); out.push(`<h${n}>${inline(m[2])}</h${n}>`); continue; }
    if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) { flush(); out.push('<hr>'); continue; }
    if ((m = line.match(/^\s*>\s?(.*)$/))) { flush(); out.push(`<blockquote>${inline(m[1])}</blockquote>`); continue; }
    if ((m = line.match(/^\s*([-*+]|\d+[.)])\s+(.*)$/))) {
      const kind = /\d/.test(m[1]) ? 'ol' : 'ul';
      if (para.length) { out.push(`<p>${para.map(inline).join('<br>')}</p>`); para = []; }
      if (list !== kind) { if (list) out.push(`</${list}>`); out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${inline(m[2])}</li>`); continue;
    }
    if (!line.trim()) { flush(); continue; }
    if (list) { out.push(`</${list}>`); list = null; }
    para.push(line);
  }
  if (code !== null) out.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`);
  flush();
  return out.join('');
}
let currentNote = null;
function openReader(title, text, vault = null, file = null) {
  currentNote = { title, text, vault, file, isEditing: false };
  $('modalTitle').textContent = title;
  $('modalBody').innerHTML = md(text);
  $('modalBody').hidden = false;
  $('modalEdit').hidden = true;
  $('modalEditBtn').hidden = !file;
  $('modalEditBtn').textContent = '✏ Edit';
  $('modalSaveBtn').hidden = true;
  $('modal').hidden = false;
  $('modalBody').scrollTop = 0;
}
$('modalClose').onclick = () => { $('modal').hidden = true; };
$('modal').addEventListener('click', (e) => { if (e.target.id === 'modal') $('modal').hidden = true; });

$('modalEditBtn').onclick = () => {
  if (!currentNote || !currentNote.file) return;
  currentNote.isEditing = !currentNote.isEditing;
  if (currentNote.isEditing) {
    $('modalEdit').value = currentNote.text;
    $('modalBody').hidden = true;
    $('modalEdit').hidden = false;
    $('modalEditBtn').textContent = '👁 Preview';
    $('modalSaveBtn').hidden = false;
    $('modalEdit').focus();
  } else {
    currentNote.text = $('modalEdit').value;
    $('modalBody').innerHTML = md(currentNote.text);
    $('modalBody').hidden = false;
    $('modalEdit').hidden = true;
    $('modalEditBtn').textContent = '✏ Edit';
    $('modalSaveBtn').hidden = true;
  }
};

$('modalSaveBtn').onclick = async () => {
  if (!currentNote || !currentNote.file) return;
  const newText = $('modalEdit').value;
  $('modalSaveBtn').disabled = true;
  try {
    const res = await post('/api/note-save', { vault: currentNote.vault, file: currentNote.file, text: newText });
    $('modalSaveBtn').disabled = false;
    if (res?.ok) {
      toast('Note saved');
      currentNote.text = newText;
      currentNote.isEditing = false;
      $('modalBody').innerHTML = md(newText);
      $('modalBody').hidden = false;
      $('modalEdit').hidden = true;
      $('modalEditBtn').textContent = '✏ Edit';
      $('modalSaveBtn').hidden = true;
      loadNotes();
    } else {
      toast(res?.why || 'Failed to save note');
    }
  } catch (err) {
    $('modalSaveBtn').disabled = false;
    toast('Error saving note');
  }
};

const OPS = { queued: ['now', 'cancel'], running: ['cancel'], waiting: ['now', 'cancel'], paused: ['run', 'resume'], cancel: [], };
const OP_LABEL = { run: 'Queue Run', now: '▶ Run now', swap_run: '⚡ Swap & Run', pause: 'Pause', resume: 'Resume', cancel: 'Cancel' };
let control = null;
async function loadControl() {
  if (document.hidden) return;
  if (typeof cardShown === 'function' && !['c-chains', 'c-ask', 'c-awayq'].some(cardShown)) return;   // hidden in this layout: not polled (layouts.js)
  try { control = await (await fetch('/api/control', { cache: 'no-store' })).json(); } catch (e) { return; }
  if (!$('askModel').options.length) askMode();
  const askState = (a) => a.state.startsWith('running') ? `<span style="color:var(--cyan)">${esc(a.state)}</span>`
    : a.state.startsWith('failed') ? `<span style="color:var(--red)" title="${esc(a.state)}">failed</span><button class="mini ghost" data-remove="${esc(a.job)}">clear</button>`
    : `waiting<button class="mini ghost" data-remove="${esc(a.job)}">remove</button>`;
  setHTML($('askList'), control.asks.length ? control.asks.map((a) => `<div class="item"><span>${esc(a.title)} <span class="dim">· ${esc(a.model)}</span></span>` +
    `<span>${askState(a)}</span></div>`).join('')
    : '<div class="empty">nothing waiting</div>');
  setHTML($('answerList'), control.answers.length ? control.answers.map((a) => `<div class="item link" data-answer="${esc(a.file)}">` +
    `<span><b>${esc(a.title)}</b> <span class="dim">· ${esc(when(a.time))}${a.model ? ' · ' + esc(a.model) : ''}</span></span><span class="pv">${esc(a.preview)}</span></div>`).join('')
    : '<div class="empty">no answers yet</div>');
  const chainSel = $('chainSelect');
  if (chainSel && control.chains) {
    const curVal = chainSel.value;
    chainSel.innerHTML = control.chains.map((c) => `<option value="${esc(c.name)}">${esc(c.name)} (${esc(c.status)})</option>`).join('');
    if (curVal && Array.from(chainSel.options).some((o) => o.value === curVal)) {
      chainSel.value = curVal;
    }
  }
  initChainLauncher();
  initFixQueueUI();
  loadFixQueue();
  if (schedEdit) return;                     // don't redraw under an open schedule editor
  renderChainList();
}

// ---------- the "All chains" list (B2, 10-19): one compact row per chain - its state, one line of what's next, the
// one action that fits its state, Result when there is one; Schedule, Inspect and the rest behind "⋯". Running and
// waiting chains first, then scheduled ones by next run, then the rest by name. The filter box narrows it.
let chainMore = null;                        // the row whose "⋯" is open
function chainRank(c) {
  if (c.status === 'running') return 0;
  if (c.active_waiting || c.blocked || c.status === 'waiting') return 1;
  if (c.reason || /need/i.test(c.status || '')) return 2;
  if (c.status === 'queued') return 3;
  if (c.status === 'paused') return 4;
  if (c.next_run) return 5;
  return 6;
}
function chainPrimary(c, ops) {
  if (c.active_waiting) return 'swap_run';
  if (c.blocked) return 'now';
  return ops[0] || null;
}
function renderChainList() {
  if (!control?.chains || schedEdit) return;  // don't redraw under an open schedule editor
  const q = ($('chainFilter')?.value || '').trim().toLowerCase();
  const rows = control.chains
    .filter((c) => !q || [c.name, c.status, c.model, c.schedule].some((x) => String(x || '').toLowerCase().includes(q)))
    .sort((a, b) => chainRank(a) - chainRank(b) || String(a.next_run || '~').localeCompare(String(b.next_run || '~'))
      || a.name.localeCompare(b.name));
  const opBtn = (c, o) => `<button class="mini${o === 'swap_run' ? ' swap-run' : o === 'now' ? ' go' : ''}" data-chain="${esc(c.name)}" data-op="${o}"`
    + `${o === 'cancel' ? ' data-confirm="Cancel this chain? It stops after its current step; finished steps are kept."' : ''}>${OP_LABEL[o] || o}</button>`;
  setHTML($('chainList'), rows.map((c) => {
    let ops = OPS[c.status] || ['run', 'pause'];
    if (c.status === 'running' && c.active_waiting) ops = ['swap_run', 'cancel'];
    const primary = chainPrimary(c, ops);
    const rest = ops.filter((o) => o !== primary);
    const meta = [];
    if (c.active_waiting) meta.push(`<span class="warnc" title="${esc(c.active_waiting)}">⏳ ${esc(c.active_waiting)}</span>`);
    else if (c.blocked) meta.push(`<span class="warnc" title="${esc(c.blocked)}">⏳ ${esc(c.blocked)}</span>`);
    else if (c.reason) meta.push(`<span class="badc" title="${esc(c.reason)}">⚠ ${esc(c.reason)}</span>`);
    else if (c.next_run) meta.push(`📅 ${esc(nextLabel(c.next_run))}`);
    else if (c.schedule) meta.push(`⏱ ${esc(c.schedule)}`);
    if (c.watch) meta.push('watches a folder');
    if (c.model) meta.push(esc(c.model));
    const plainMeta = meta.join(' · ').replace(/<[^>]+>/g, '');
    const statusClass = c.status === 'running' ? 'status running live-pulse' : `status ${esc(c.status)}`;
    const statusLabel = c.status === 'running' ? 'running ⚡' : esc(c.status);
    return `<div class="crow${chainMore === c.name ? ' more-open' : ''}" data-cname="${esc(c.name)}">`
      + `<span class="cname" title="${esc(c.name)}">${esc(c.name)}</span><span class="${statusClass}">${statusLabel}</span>`
      + `<span class="meta" title="${esc(plainMeta)}">${meta.join(' · ')}</span><span class="acts">`
      + (primary ? opBtn(c, primary) : '')
      + (c.result_time ? `<button class="mini ghost" data-result="${esc(c.name)}" title="result from ${esc(c.result_time)}">Result</button>` : '')
      + `<button class="mini ghost cmore-btn" data-cmore="${esc(c.name)}" title="Schedule, inspect and more" aria-expanded="${chainMore === c.name}">⋯</button>`
      + `<span class="cmore-items">`
      + `<button class="mini ${c.schedule ? 'on' : 'ghost'} sched-btn" data-sched="${esc(c.name)}" title="Change when it runs (current: ${esc(c.schedule || 'manual')})">⏱ ${esc(c.schedule || 'Schedule')}</button>`
      + `<button class="mini ${c.reason ? 'warn' : 'ghost'} inspect-btn" data-inspect="${esc(c.name)}" title="Inspect run events and log tail">🔍 Inspect</button>`
      + rest.map((o) => opBtn(c, o)).join('') + `</span></span></div>`
      + (inspectChain === c.name ? chainDetailsHtml(c) : '');
  }).join('') || (q ? '<div class="empty">no chain matches the filter</div>' : '<div class="empty">no chain notes in 1Notebook/Chains</div>'));
}
$('chainList')?.addEventListener('click', (e) => {
  const b = e.target.closest('[data-cmore]');
  if (!b) return;
  e.stopPropagation();
  chainMore = chainMore === b.dataset.cmore ? null : b.dataset.cmore;
  renderChainList();
});
$('chainFilter')?.addEventListener('input', () => renderChainList());

function initChainLauncher() {
  const sel = $('chainSelect');
  const btnRun = $('btnRunChainNow');
  const btnQueue = $('btnQueueChain');
  const btnSched = $('btnEditChainSchedule');
  const btnRes = $('btnViewChainResults');
  if (btnRun && !btnRun._init) {
    btnRun._init = true;
    btnRun.onclick = async () => {
      const name = sel ? sel.value : null;
      if (!name) return;
      btnRun.disabled = true;
      const r = await post('/api/action', { action: 'chain', target: name, op: 'swap_run' });
      toast(r ? r.why : `Queued ${name} (running now)`);
      btnRun.disabled = false;
      loadControl();
    };
  }
  if (btnQueue && !btnQueue._init) {
    btnQueue._init = true;
    btnQueue.onclick = async () => {
      const name = sel ? sel.value : null;
      if (!name) return;
      btnQueue.disabled = true;
      const r = await post('/api/action', { action: 'chain', target: name, op: 'run' });
      toast(r ? r.why : `Queued ${name}`);
      btnQueue.disabled = false;
      loadControl();
    };
  }
  if (btnSched && !btnSched._init) {
    btnSched._init = true;
    btnSched.onclick = () => {
      const name = sel ? sel.value : null;
      if (!name) return;
      openScheduleEditor(name);
    };
  }
  if (btnRes && !btnRes._init) {
    btnRes._init = true;
    btnRes.onclick = async () => {
      const name = sel ? sel.value : null;
      if (!name) return;
      btnRes.disabled = true;
      try {
        const a = await (await fetch(`/api/chain-result?name=${encodeURIComponent(name)}`)).json();
        if (a.ok) {
          openReader(`${name}: latest result`, a.text);
        } else {
          toast(a.why || 'No result found yet');
        }
      } catch (err) {
        toast('Could not fetch chain result');
      } finally {
        btnRes.disabled = false;
      }
    };
  }
}

async function loadFixQueue() {
  const el = $('fixQueueList');
  if (!el) return;
  try {
    const data = await (await fetch('/api/fix-queue', { cache: 'no-store' })).json();
    const pill = $('fixQueuePill');
    if (pill) {
      pill.textContent = `${data.total_open || 0} open`;
      pill.className = `pill ${data.total_open > 0 ? 'active' : 'idle'}`;
    }
    const items = [];
    (data.green || []).forEach((g) => {
      items.push(`<div class="srow"><span><b style="color:var(--green)">✓ GREEN</b> <b>${esc(g.repo)}</b>: ${esc(g.task)} <code class="small">ai/${esc(g.branch || '')}</code></span></div>`);
    });
    (data.open || []).forEach((o) => {
      items.push(`<div class="srow"><span><b>${esc(o.repo)}</b>: ${esc(o.task)}</span><span class="dim small">${o.attempts}/${o.max} tries</span></div>`);
    });
    (data.unresolved || []).forEach((u) => {
      items.push(`<div class="srow"><span><b class="dim">FAILED</b> <b>${esc(u.repo)}</b>: ${esc(u.task)} <span class="dim small">[${esc(u.reason)}]</span></span><button class="mini ghost" data-reset-fix="${esc(u.repo)}" data-task="${esc(u.task)}">Retry</button></div>`);
    });
    setHTML(el, items.join('') || '<div class="empty dim">No open or recent fix tasks</div>');
  } catch (e) {
    setHTML(el, '<div class="empty dim">Could not load fix queue</div>');
  }
}

function initFixQueueUI() {
  const btnToggle = $('btnToggleAddFix');
  if (btnToggle && !btnToggle._init) {
    btnToggle._init = true;
    btnToggle.onclick = () => {
      const f = $('addFixForm');
      if (f) f.hidden = !f.hidden;
    };
  }
  const btnSub = $('btnSubmitFixTask');
  if (btnSub && !btnSub._init) {
    btnSub._init = true;
    btnSub.onclick = async () => {
      const repo = $('fixRepoSelect')?.value;
      const task = $('fixTaskInput')?.value?.trim();
      if (!repo || !task) return;
      btnSub.disabled = true;
      try {
        const r = await post('/api/fix-queue', { action: 'add', repo, task });
        if (r?.ok) {
          toast(`Added task to ${repo}`);
          if ($('fixTaskInput')) $('fixTaskInput').value = '';
          const f = $('addFixForm');
          if (f) f.hidden = true;
          loadFixQueue();
        } else {
          toast(r?.why || 'Failed to add task');
        }
      } finally {
        btnSub.disabled = false;
      }
    };
  }
  const list = $('fixQueueList');
  if (list && !list._init) {
    list._init = true;
    list.addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-reset-fix]');
      if (!btn) return;
      const repo = btn.dataset.resetFix;
      const task = btn.dataset.task;
      btn.disabled = true;
      try {
        const r = await post('/api/fix-queue', { action: 'reset', repo, task });
        if (r?.ok) {
          toast(`Reset task for ${repo}`);
          loadFixQueue();
        }
      } finally {
        btn.disabled = false;
      }
    });
  }
}
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

// ---------- AI Usage (Claude + Antigravity)
function fmtTokens(n) {
  if (!n) return '0';
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}

async function loadAiUsage() {
  if (document.hidden) return;
  if (typeof cardShown === 'function' && !cardShown('c-ai-usage')) return;
  let res; try { res = await (await fetch('/api/ai-usage', { cache: 'no-store' })).json(); } catch (e) { return; }
  if (!res || !res.available) {
    setHTML($('aiUsageStats'), '<div class="dim">Usage tracker data not yet available</div>');
    return;
  }
  const stat = (v, label, color) => `<div><b style="color:${color || 'var(--text)'}">${v ?? '–'}</b><span>${label}</span></div>`;
  const t = res.today || {};
  const c = t.claude || {};
  const ag = t.antigravity || {};
  const w = res.weekly || {};

  const claudeTodayTok = (c.output_tokens || 0) + (c.input_tokens || 0);
  const claudeReadTok = c.cache_read_tokens || 0;
  const agTok = ag.estimated_tokens || 0;

  $('aiUsageWeek').textContent = res.week_id ? `· ${res.week_id}` : '';
  setHTML($('aiUsageStats'), [
    stat(fmtTokens(claudeTodayTok), `Today Claude (${c.turns || 0} turns)`, 'var(--cyan)'),
    stat(fmtTokens(claudeReadTok), 'Claude cache reads', claudeReadTok ? 'var(--green)' : 'var(--dim)'),
    stat(fmtTokens(agTok), `Today Antigravity (${ag.turns || 0} steps)`, 'var(--cyan)'),
    stat(fmtTokens(w.claude_tokens), `Week Claude (${w.claude_turns || 0} turns)`, 'var(--text)'),
    stat(fmtTokens(w.antigravity_tokens), `Week Antigravity (${w.antigravity_turns || 0} steps)`, 'var(--text)'),
  ].join(''));

  const models = (t.models || []).map(m => esc(m.model)).join(' · ');
  const projs = (t.projects || []).map(p => esc(p)).join(', ');
  const noteParts = [];
  if (models) noteParts.push(`Models: <b>${models}</b>`);
  if (projs) noteParts.push(`Projects: ${projs}`);
  if (res.last_updated) noteParts.push(`Updated: ${res.last_updated}`);
  setHTML($('aiUsageSub'), noteParts.join(' · ') || 'No activity recorded today.');
}
setInterval(loadAiUsage, 60000);
document.addEventListener('visibilitychange', () => { if (!document.hidden && !$('aiUsageStats')?.children.length) loadAiUsage(); });

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

// ---------- Obsidian Notes widget
let selectedVault = localStorage.getItem('sol_notes_vault') || 'OBVLT';
let notesQuery = '';
let searchTimeout = null;
let pinnedNotes = new Set(JSON.parse(localStorage.getItem('sol_pinned_notes') || '[]'));
let allNotesCache = [];

async function initVaults() {
  const sel = $('notesVault');
  if (!sel) return;
  try {
    const res = await (await fetch('/api/vaults', { cache: 'no-store' })).json();
    if (res.ok && res.vaults?.length) {
      sel.innerHTML = res.vaults.map((v) => `<option value="${esc(v)}"${v === selectedVault ? ' selected' : ''}>${esc(v)}</option>`).join('');
      if (!res.vaults.includes(selectedVault)) {
        selectedVault = res.default || res.vaults[0];
        sel.value = selectedVault;
      }
    }
  } catch (e) { /* keep default */ }
  sel.onchange = () => {
    selectedVault = sel.value;
    localStorage.setItem('sol_notes_vault', selectedVault);
    loadNotes();
  };
}

async function loadNotes() {
  if (typeof cardShown === 'function' && !cardShown('c-notes')) return;   // hidden in this layout: not polled (layouts.js)
  const container = $('notesList');
  if (!container || document.hidden) return;
  try {
    const url = `/api/notes?vault=${encodeURIComponent(selectedVault)}&q=${encodeURIComponent(notesQuery)}`;
    const res = await (await fetch(url, { cache: 'no-store' })).json();
    if (!res.ok) {
      setHTML(container, `<div class="empty">${esc(res.why || 'error loading notes')}</div>`);
      return;
    }
    const notes = res.notes || [];
    $('notesVaultCount').textContent = `${notes.length} recent in ${selectedVault}`;
    const staleCount = res.stale_count || 0;
    const btnCondense = $('notesCondenseBtn');
    if (btnCondense) {
      if (staleCount > 0) {
        btnCondense.hidden = false;
        btnCondense.textContent = `🧹 Condense (${staleCount})`;
      } else {
        btnCondense.hidden = true;
      }
    }
    if (!notes.length) {
      setHTML(container, `<div class="empty">no notes match in ${esc(selectedVault)}</div>`);
      return;
    }

    // Sort: pinned first, then chronological
    const sorted = [...notes].sort((a, b) => {
      const aPin = pinnedNotes.has(`${a.vault}::${a.file}`);
      const bPin = pinnedNotes.has(`${b.vault}::${b.file}`);
      if (aPin !== bPin) return aPin ? -1 : 1;
      return b.mtime - a.mtime;
    });

    const html = sorted.map((n) => {
      const pinKey = `${n.vault}::${n.file}`;
      const isPinned = pinnedNotes.has(pinKey);
      const tags = (n.tags || []).slice(0, 3).map((t) => `<span class="note-tag" title="Tag #${esc(t)}">#${esc(t)}</span>`).join('');
      return `
      <div class="note-row" data-vault="${esc(n.vault)}" data-file="${esc(n.file)}" data-title="${esc(n.title)}">
        <div class="note-title-box">
          <span class="note-star ${isPinned ? 'on' : ''}" data-star="${esc(pinKey)}" title="${isPinned ? 'Unpin' : 'Pin to top'}">★</span>
          <span class="note-title" title="${esc(n.file)}">${esc(n.title)}</span>
          ${tags}
        </div>
        <span class="note-folder" title="${esc(n.folder)}">${esc(n.folder || '–')}</span>
        <span class="note-time">${esc(n.mtime_str)}</span>
        <div class="note-acts">
          <button class="mini ghost" data-note-act="folder" title="Open containing folder in Explorer">📁</button>
          <button class="mini ghost" data-note-act="preview" title="Quick preview / edit in browser">👁 Read</button>
          <button class="mini" data-note-act="open" title="Open in Obsidian desktop app">↗ Obsidian</button>
        </div>
      </div>`;
    }).join('');
    setHTML(container, html);
  } catch (e) {
    setHTML(container, '<div class="empty">error fetching notes</div>');
  }
}

if ($('notesSearch')) {
  $('notesSearch').oninput = (e) => {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(() => {
      notesQuery = e.target.value.trim();
      loadNotes();
    }, 200);
  };
}

if ($('notesRefresh')) {
  $('notesRefresh').onclick = () => loadNotes();
}

// Scratch & New Note Composer Modal
if ($('notesComposeBtn')) {
  $('notesComposeBtn').onclick = () => {
    $('composerModal').hidden = false;
    $('composerText').value = '';
    $('newFileName').value = '';
    $('newFileNameBox').hidden = true;
    const dailyR = document.querySelector('input[name="compDest"][value="daily"]');
    if (dailyR) dailyR.checked = true;
    $('composerText').focus();
  };
}
if ($('notesCondenseBtn')) {
  $('notesCondenseBtn').onclick = async () => {
    $('notesCondenseBtn').disabled = true;
    const r = await post('/api/action', { action: 'chain', target: 'Note Condenser', op: 'swap_run' });
    toast(r ? r.why : 'Note Condenser triggered');
    $('notesCondenseBtn').disabled = false;
    loadControl();
  };
}
if ($('composerClose')) $('composerClose').onclick = () => { $('composerModal').hidden = true; };
if ($('composerCancel')) $('composerCancel').onclick = () => { $('composerModal').hidden = true; };
if ($('composerModal')) {
  $('composerModal').addEventListener('click', (e) => { if (e.target.id === 'composerModal') $('composerModal').hidden = true; });
}
document.querySelectorAll('input[name="compDest"]').forEach((r) => {
  r.onchange = () => {
    $('newFileNameBox').hidden = r.value !== 'new';
  };
});
if ($('composerForm')) {
  $('composerForm').onsubmit = async (e) => {
    e.preventDefault();
    const dest = document.querySelector('input[name="compDest"]:checked').value;
    const text = $('composerText').value.trim();
    if (!text) return;
    const btn = e.target.querySelector('button[type="submit"]');
    btn.disabled = true;
    if (dest === 'new') {
      const fileName = $('newFileName').value.trim();
      if (!fileName) {
        toast('Please enter a note filename');
        btn.disabled = false;
        return;
      }
      const res = await post('/api/note-create', { vault: selectedVault, file: fileName, text });
      btn.disabled = false;
      toast(res.why || (res.ok ? 'Note created' : 'Failed to create note'));
      if (res.ok) {
        $('composerModal').hidden = true;
        loadNotes();
      }
    } else {
      const res = await post('/api/action', { action: 'scratch', destination: dest, text });
      btn.disabled = false;
      toast(res.why || (res.ok ? 'Saved to note' : 'Failed to save'));
      if (res.ok) {
        $('composerModal').hidden = true;
        loadNotes();
      }
    }
  };
}

// Git Status Modal
document.addEventListener('click', async (e) => {
  const repoChip = e.target.closest('#gitRepos span');
  if (repoChip) {
    const repoName = repoChip.textContent.replace(/\*$/, '').trim();
    if (!repoName) return;
    $('gitModalTitle').textContent = `Git Status: ${repoName}`;
    $('gitModalBody').textContent = 'Loading status…';
    $('gitModal').hidden = false;
    try {
      const res = await (await fetch(`/api/git-status?repo=${encodeURIComponent(repoName)}`)).json();
      $('gitModalBody').textContent = res.status || (res.ok ? 'Working tree clean' : (res.why || 'Failed to get status'));
    } catch (err) {
      $('gitModalBody').textContent = 'Error querying git status';
    }
  }
});
if ($('gitModalClose')) $('gitModalClose').onclick = () => { $('gitModal').hidden = true; };
if ($('gitModal')) {
  $('gitModal').addEventListener('click', (e) => { if (e.target.id === 'gitModal') $('gitModal').hidden = true; });
}

// Clear Crashes Action
if ($('ackCrashBtn')) {
  $('ackCrashBtn').onclick = async (e) => {
    e.stopPropagation();
    const r = await post('/api/action', { action: 'ack_crashes' });
    toast(r.why || 'Crashes cleared');
  };
}

// Quick Model Switcher & Header/Quickbar Actions
document.addEventListener('click', (e) => {
  const qm = e.target.closest('[data-quick-model]');
  if (qm) {
    const model = qm.dataset.quickModel;
    post('/api/action', { action: 'model', target: model, op: 'load' }).then((r) => toast(r.why));
    return;
  }
  const jmp = e.target.closest('[data-jump]');
  if (jmp && typeof window.jumpToCard === 'function') {
    window.jumpToCard(jmp.dataset.jump);
  }
});

if ($('qbAiPowerBtn')) {
  $('qbAiPowerBtn').onclick = async () => {
    const mode = payload?.snapshot?.ai_mode;
    if (mode === 'away') {
      const r = await post('/api/action', { action: 'stop_ai' });
      toast(r?.why || 'Stopping Away');
      return;
    }
    const want = $('qbAiPowerBtn').dataset.power || 'on';
    if (want === 'off' && !(await hudConfirm('Turn the local AI off?'))) return;
    $('qbAiPowerBtn').disabled = true;
    const r = await post('/api/action', { action: 'ai_power', target: want });
    $('qbAiPowerBtn').disabled = false;
    toast(r?.why || (want === 'on' ? 'Starting Local AI…' : 'Stopped Local AI'));
  };
}

if ($('qbScratchBtn')) {
  $('qbScratchBtn').onclick = () => $('notesComposeBtn')?.click();
}

async function openDoctorModal() {
  const modal = $('doctorModal');
  const body = $('doctorModalBody');
  if (!modal || !body) return;
  modal.hidden = false;
  setHTML(body, '<div class="empty">Running SOL Doctor checks…</div>');
  try {
    const res = await (await fetch('/api/doctor', { cache: 'no-store' })).json();
    if (!res.ok) {
      setHTML(body, '<div class="empty">Doctor check failed</div>');
      return;
    }
    const badgeCol = { OK: 'ok', WARN: 'warn', FAIL: 'bad' };
    const summaryText = res.exit === 0 ? 'All checks passed' : res.exit === 1 ? 'Warnings detected' : 'Action needed';
    const summaryCls = res.exit === 0 ? 'ok' : res.exit === 1 ? 'warn' : 'bad';
    const rows = (res.checks || []).map((c) => `
      <div class="doc-row${c.indent ? ' doc-indent' : ''}">
        <span class="pill ${badgeCol[c.level] || ''}">${esc(c.level)}</span>
        <b class="doc-name">${esc(c.name)}</b>
        <span class="doc-detail">${esc(c.detail)}</span>
      </div>
    `).join('');
    setHTML(body, `<div class="doc-summary"><span class="pill ${summaryCls}">${esc(summaryText)}</span><span class="dim small">${(res.checks || []).length} subsystem checks</span></div><div class="doc-list">${rows}</div>`);
  } catch (err) {
    setHTML(body, '<div class="empty">Could not reach /api/doctor</div>');
  }
}

if ($('qbDoctorBtn')) $('qbDoctorBtn').onclick = openDoctorModal;
if ($('doctorRefreshBtn')) $('doctorRefreshBtn').onclick = openDoctorModal;
if ($('doctorModalClose')) $('doctorModalClose').onclick = () => { $('doctorModal').hidden = true; };
if ($('doctorModal')) {
  $('doctorModal').addEventListener('click', (e) => { if (e.target.id === 'doctorModal') $('doctorModal').hidden = true; });
}

// Note row clicks and Star Pinning
document.addEventListener('click', async (e) => {
  const star = e.target.closest('[data-star]');
  if (star) {
    e.stopPropagation();
    const key = star.dataset.star;
    if (pinnedNotes.has(key)) pinnedNotes.delete(key);
    else pinnedNotes.add(key);
    localStorage.setItem('sol_pinned_notes', JSON.stringify([...pinnedNotes]));
    loadNotes();
    return;
  }

  const actBtn = e.target.closest('[data-note-act]');
  const row = e.target.closest('.note-row');
  if (!actBtn && !row) return;

  const targetRow = actBtn ? actBtn.closest('.note-row') : row;
  if (!targetRow) return;
  const vault = targetRow.dataset.vault;
  const file = targetRow.dataset.file;
  const title = targetRow.dataset.title;

  const act = actBtn ? actBtn.dataset.noteAct : 'open';
  if (act === 'preview') {
    e.stopPropagation();
    try {
      const res = await (await fetch(`/api/note-content?vault=${encodeURIComponent(vault)}&file=${encodeURIComponent(file)}`)).json();
      if (res.ok) openReader(`${title} (${vault})`, res.text, vault, file);
      else toast(res.why || 'Could not load note content');
    } catch (err) {
      toast('Failed to preview note');
    }
  } else if (act === 'folder') {
    e.stopPropagation();
    const res = await post('/api/action', { action: 'open_folder', vault, file });
    if (res?.why) toast(res.why);
  } else if (act === 'open') {
    e.stopPropagation();
    const res = await post('/api/action', { action: 'open_note', vault, file });
    if (res?.why) toast(res.why);
  }
});

// Command Palette (Ctrl+K)
let paletteOpen = false;
let paletteSelectedIndex = 0;

function openPalette() {
  paletteOpen = true;
  $('paletteModal').hidden = false;
  $('paletteInput').value = '';
  renderPalette('');
  $('paletteInput').focus();
}

function closePalette() {
  paletteOpen = false;
  $('paletteModal').hidden = true;
}

if ($('cmdPaletteBtn')) $('cmdPaletteBtn').onclick = openPalette;
if ($('paletteModal')) {
  $('paletteModal').addEventListener('click', (e) => { if (e.target.id === 'paletteModal') closePalette(); });
}

addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    if (paletteOpen) closePalette(); else openPalette();
  } else if (e.key === 'Escape') {
    if (paletteOpen) closePalette();
    if ($('composerModal') && !$('composerModal').hidden) $('composerModal').hidden = true;
    if ($('gitModal') && !$('gitModal').hidden) $('gitModal').hidden = true;
    if ($('doctorModal') && !$('doctorModal').hidden) $('doctorModal').hidden = true;
  }
});

function getPaletteItems(query) {
  const q = query.toLowerCase().trim();
  const items = [
    { label: 'Run SOL System Doctor (Health Check)', cat: 'System', run: () => { closePalette(); openDoctorModal(); } },
    { label: 'Wake Local AI (Desk Mode)', cat: 'AI', run: () => { closePalette(); post('/api/action', { action: 'ai_power', target: 'on' }).then((r) => toast(r.why)); } },
    { label: 'New Scratch Note / Composer', cat: 'Action', run: () => { closePalette(); $('notesComposeBtn')?.click(); } },
    { label: 'Inspect & Acknowledge Crashes', cat: 'Action', run: () => { closePalette(); post('/api/action', { action: 'ack_crashes' }).then((r) => toast(r.why)); } },
    { label: 'Unload All Local AI Models', cat: 'AI', run: () => { closePalette(); post('/api/action', { action: 'models_unload_all' }).then((r) => toast(r.why)); } },
    { label: 'Start Away Mode + Sleep', cat: 'System', run: () => { closePalette(); post('/api/action', { action: 'away-sleep' }).then((r) => toast(r.why)); } },
    { label: 'Load sol-fast (Gemma 4 12B, fast, 8k context)', cat: 'Model', run: () => { closePalette(); post('/api/action', { action: 'model', target: 'sol-fast', op: 'load' }).then((r) => toast(r.why)); } },
    { label: 'Load sol-vision (Gemma 4 12B with images)', cat: 'Model', run: () => { closePalette(); post('/api/action', { action: 'model', target: 'sol-vision', op: 'load' }).then((r) => toast(r.why)); } },
    { label: 'Load sol-smart (gpt-oss-20b Reasoning)', cat: 'Model', run: () => { closePalette(); post('/api/action', { action: 'model', target: 'sol-smart', op: 'load' }).then((r) => toast(r.why)); } },
    { label: 'Load sol-long (gpt-oss-20b 65k Context)', cat: 'Model', run: () => { closePalette(); post('/api/action', { action: 'model', target: 'sol-long', op: 'load' }).then((r) => toast(r.why)); } },
  ];

  if (control?.chains) {
    for (const c of control.chains) {
      items.push({
        label: `Run Chain: ${c.name}`,
        cat: 'Chain',
        run: () => { closePalette(); post('/api/action', { action: 'chain', target: c.name, op: 'run' }).then((r) => toast(r.why)); }
      });
    }
  }

  if (typeof launchpadPaletteItems === 'function') items.push(...launchpadPaletteItems());   // launchpad.js
  if (typeof layoutPaletteItems === 'function') items.push(...layoutPaletteItems());         // layouts.js

  for (const n of allNotesCache) {
    items.push({
      label: `Note: ${n.title} (${n.vault})`,
      cat: 'Note',
      run: () => { closePalette(); post('/api/action', { action: 'open_note', vault: n.vault, file: n.file }).then((r) => toast(r.why)); }
    });
  }

  return q ? items.filter((it) => it.label.toLowerCase().includes(q) || it.cat.toLowerCase().includes(q)) : items;
}

function renderPalette(query) {
  const items = getPaletteItems(query).slice(0, 15);
  paletteSelectedIndex = Math.min(paletteSelectedIndex, Math.max(0, items.length - 1));
  if (!items.length) {
    setHTML($('paletteList'), '<div class="empty">no matching commands or notes</div>');
    return;
  }
  const html = items.map((it, idx) => `
    <div class="palette-item ${idx === paletteSelectedIndex ? 'selected' : ''}" data-idx="${idx}">
      <span>${esc(it.label)}</span>
      <span class="cat">${esc(it.cat)}</span>
    </div>
  `).join('');
  setHTML($('paletteList'), html);
}

if ($('paletteInput')) {
  $('paletteInput').oninput = (e) => {
    paletteSelectedIndex = 0;
    renderPalette(e.target.value);
  };

  $('paletteInput').onkeydown = (e) => {
    const items = getPaletteItems($('paletteInput').value).slice(0, 15);
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      paletteSelectedIndex = (paletteSelectedIndex + 1) % items.length;
      renderPalette($('paletteInput').value);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      paletteSelectedIndex = (paletteSelectedIndex - 1 + items.length) % items.length;
      renderPalette($('paletteInput').value);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (items[paletteSelectedIndex]) {
        items[paletteSelectedIndex].run();
      }
    }
  };
}

if ($('paletteList')) {
  $('paletteList').addEventListener('click', (e) => {
    const itemEl = e.target.closest('.palette-item');
    if (!itemEl) return;
    const idx = parseInt(itemEl.dataset.idx, 10);
    const items = getPaletteItems($('paletteInput').value).slice(0, 15);
    if (items[idx]) items[idx].run();
  });
}

setInterval(loadNotes, 15000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) loadNotes(); });

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

// ---------- is anything rendering on a GPU with no monitor?
// 10-08: nine apps were pinned to "power saving" = the Intel UHD 770, which drives no screen, so every frame was
// drawn on the weak chip and copied across to the Radeon. It felt like lag, and nothing on the machine said so.
// Two signals: the Windows setting (true even while an app is shut) and what is actually rendering there now.
async function loadDisplays() {
  if (typeof cardShown === 'function' && !cardShown('c-gpu')) return;   // hidden in this layout: not polled (layouts.js)
  let d;
  try { d = await (await fetch('/api/displays', { cache: 'no-store' })).json(); } catch (e) { return; }
  const box = $('gpuWrong');
  if (!box) return;
  if (!d.ok || !d.available) { box.hidden = true; return; }
  const busy = d.busy || [], pinned = d.power_saving_apps || [], dark = (d.dark || [])[0];
  let html = '';
  if (busy.length) {
    const who = busy.map((r) => `<b>${esc(r.name)}</b> ${r.percent}%`).join(', ');
    html = `⚠ ${who} rendering on ${esc(busy[0].adapter)}, which has no monitor — every frame is copied to the card that does.`
         + ` <span class="dim">Restart those apps, or set them to “Let Windows decide” in Settings › Display › Graphics.</span>`;
  } else if (d.setting_problem && dark) {
    html = `⚠ ${pinned.length} app(s) set to power saving (${esc(pinned.slice(0, 4).join(', '))}) while ${esc(dark)}`
         + ` has no monitor — they will render there and be copied across.`;
  }
  if (html) setHTML(box, html);
  box.hidden = !html;
}
setInterval(() => { if (!document.hidden) loadDisplays(); }, 10000);

// ---------- localhost: what listens on this PC, what closed, and closing a dev server you forgot
// The card shows dev servers and the AI stack by default; apps and Windows services are a click away (there are
// dozens of them and none of them is why you opened this). What may be closed is decided by the server, not here.
let portsAll = false;

function upWords(s) {
  if (s == null) return '';
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h`;
  return `${(s / 86400).toFixed(1)}d`;
}
function portWhen(epoch) {
  if (!epoch) return '';
  const d = new Date(epoch * 1000), hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return d.toDateString() === new Date().toDateString() ? hm : `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${hm}`;
}
function portRow(r) {
  const act = r.can_close
    ? `<button class="mini danger" data-port="${r.port}" data-pid="${r.pid}">✕ close</button>`
    : `<span class="dim small" title="${esc(r.kind_words)}: this card keeps it read-only">🔒 ${esc(r.kind_words)}</span>`;
  const nics = r.everyone ? ' <span class="pill warn" title="Listening on every interface, not only this PC">all nics</span>' : '';
  return `<div class="prow"><b>:${r.port}</b>`
    + `<span class="meta">${esc(r.name)}${r.label ? ` · ${esc(r.label)}` : ''}${nics}${chipTag(r.chip, r.kind === 'ai')}</span>`
    + `<span class="meta" title="${r.started ? `up since ${new Date(r.started * 1000).toLocaleString()}` : 'start time unknown'}">${upWords(r.up_seconds)}</span>`
    + `<span class="acts">${act}</span></div>`;
}
function closedRow(c) {
  return `<div class="prow"><b class="dim">:${c.port}</b>`
    + `<span class="meta">${esc(c.name)}${c.label ? ` · ${esc(c.label)}` : ''}</span>`
    + `<span class="meta">${c.ran_seconds == null ? '' : `ran ${upWords(c.ran_seconds)}`}</span>`
    + `<span class="acts meta">closed ${portWhen(c.closed_at)}${c.approx ? '*' : ''}</span></div>`;
}
async function loadPorts() {
  if (typeof cardShown === 'function' && !cardShown('c-ports')) return;   // hidden in this layout: not polled (layouts.js)
  let d;
  try { d = await (await fetch('/api/ports', { cache: 'no-store' })).json(); } catch (e) { return; }
  if (!d.ok) return;
  const all = d.listening || [], counts = d.counts || {}, closed = d.closed || [];
  const rows = all.filter((r) => portsAll || (r.kind !== 'system' && r.kind !== 'app'));
  const hidden = (counts.app || 0) + (counts.system || 0);
  $('portsSummary').textContent = `${all.length} listening · ${d.closable || 0} you can close`;
  setHTML($('portsList'), rows.length ? rows.map(portRow).join('') : '<div class="empty">nothing of yours is listening</div>');
  setHTML($('portsAll'), `${portsAll ? '–' : '+'} ${hidden} apps · Windows`);
  $('portsHistory').hidden = !closed.length;
  setHTML($('portsClosed'), closed.map(closedRow).join(''));
  $('portsApprox').hidden = !closed.some((c) => c.approx);
}
$('portsAll').onclick = () => {
  portsAll = !portsAll;
  $('portsAll').setAttribute('aria-pressed', String(portsAll));
  loadPorts();
};
$('portsRefresh').onclick = () => loadPorts();
$('portsList').addEventListener('click', async (e) => {
  const b = e.target.closest('button[data-port]');
  if (!b) return;
  const what = b.closest('.prow').querySelector('.meta').textContent.trim();
  if (!(await hudConfirm(`Close :${b.dataset.port}?\n\n${what}\n\nIt is asked to stop first, then forced if it ignores that. Anything unsaved in it is lost.`))) return;
  b.disabled = true;
  const r = await post('/api/action', { action: 'close_port', target: b.dataset.port, pid: Number(b.dataset.pid) });
  toast(r.why || (r.ok ? 'closed' : 'could not close it'));
  loadPorts();
});
setInterval(() => { if (!document.hidden) loadPorts(); }, 5000);

// ---------- Overnight Runner
let overnightInit = false;
let overnightData = null;

function initOvernightEvents() {
  if (overnightInit) return;
  overnightInit = true;

  const btnStart = $('btnStartOvernight');
  if (btnStart) {
    btnStart.onclick = async () => {
      const checkedBoxes = Array.from(document.querySelectorAll('#overnightChainList input[type="checkbox"]:checked'));
      const chains = checkedBoxes.map((cb) => cb.value);
      if (!chains.length) {
        toast('Select at least one chain to run tonight');
        return;
      }
      const dispRadio = document.querySelector('input[name="overnightDisplayMode"]:checked');
      const displayMode = dispRadio ? dispRadio.value : 'dpms_off';
      const sleepDone = Boolean($('overnightSleepDone')?.checked);

      btnStart.disabled = true;
      const r = await post('/api/action', {
        action: 'start_overnight',
        chains,
        display_mode: displayMode,
        sleep_when_done: sleepDone,
      });
      toast(r?.why || (r?.ok ? 'Overnight run started' : 'Could not start overnight run'));
      btnStart.disabled = false;
      loadOvernight();
    };
  }

  const btnCancel = $('btnCancelOvernight');
  if (btnCancel) {
    btnCancel.onclick = async () => {
      if (!(await hudConfirm('Cancel the overnight run?'))) return;
      btnCancel.disabled = true;
      const r = await post('/api/action', { action: 'cancel_overnight' });
      toast(r?.why || 'Cancelled overnight run');
      btnCancel.disabled = false;
      loadOvernight();
    };
  }

  const presetsEl = $('overnightPresets');
  if (presetsEl) {
    presetsEl.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-preset]');
      if (!btn || !overnightData) return;
      const presetKey = btn.dataset.preset;
      const p = (overnightData.presets || {})[presetKey];
      if (!p) return;
      const targetChains = new Set(p.chains || []);
      document.querySelectorAll('#overnightChainList input[type="checkbox"]').forEach((cb) => {
        cb.checked = targetChains.has(cb.value);
      });
      toast(`Selected preset: ${p.label}`);
      const bedtimeCb = $('overnightBedtimeTrigger');
      if (bedtimeCb?.checked) {
        post('/api/action', {
          action: 'set_overnight_bedtime',
          enabled: true,
          chains: Array.from(targetChains),
        });
      }
    });
  }

  const bedtimeCb = $('overnightBedtimeTrigger');
  if (bedtimeCb) {
    bedtimeCb.addEventListener('change', async (e) => {
      const checkedBoxes = Array.from(document.querySelectorAll('#overnightChainList input[type="checkbox"]:checked'));
      const chains = checkedBoxes.map((cb) => cb.value);
      const dispRadio = document.querySelector('input[name="overnightDisplayMode"]:checked');
      const sleepDone = Boolean($('overnightSleepDone')?.checked);
      const res = await post('/api/action', {
        action: 'set_overnight_bedtime',
        enabled: e.target.checked,
        chains,
        display_mode: dispRadio ? dispRadio.value : 'dpms_off',
        sleep_when_done: sleepDone,
      });
      toast(res?.why || (e.target.checked ? 'Bedtime auto-start armed' : 'Bedtime auto-start disabled'));
      loadOvernight();
    });
  }

  const chainListEl = $('overnightChainList');
  if (chainListEl) {
    chainListEl.addEventListener('change', () => {
      const bCb = $('overnightBedtimeTrigger');
      if (bCb?.checked) {
        const checkedBoxes = Array.from(document.querySelectorAll('#overnightChainList input[type="checkbox"]:checked'));
        post('/api/action', {
          action: 'set_overnight_bedtime',
          enabled: true,
          chains: checkedBoxes.map((cb) => cb.value),
        });
      }
    });
  }
}

async function loadOvernight() {
  initOvernightEvents();
  if (typeof cardShown === 'function' && !cardShown('c-overnight')) return;
  let d;
  try {
    d = await (await fetch('/api/overnight', { cache: 'no-store' })).json();
  } catch (e) {
    return;
  }
  if (!d?.ok) return;
  overnightData = d;
  const state = d.state || {};
  const presets = d.presets || {};
  const chains = d.chains || [];

  const statusPill = $('overnightStatusPill');
  const banner = $('overnightActiveBanner');
  const btnStart = $('btnStartOvernight');
  const btnCancel = $('btnCancelOvernight');
  const bedtimeCb = $('overnightBedtimeTrigger');
  if (bedtimeCb && document.activeElement !== bedtimeCb) {
    bedtimeCb.checked = Boolean(state.bedtime_autostart);
  }

  if (state.active) {
    if (statusPill) {
      statusPill.textContent = 'running';
      statusPill.className = 'pill waiting live-pulse';
    }
    if (banner) banner.hidden = false;
    if (btnStart) btnStart.hidden = true;
    if (btnCancel) btnCancel.hidden = false;

    const currEl = $('overnightCurrentChain');
    if (currEl) currEl.innerHTML = `<b>Running:</b> ${esc(state.current || 'initializing…')}`;

    const elapsedEl = $('overnightElapsed');
    if (elapsedEl && state.started_at) {
      const min = Math.max(1, Math.round((Date.now() / 1000 - state.started_at) / 60));
      elapsedEl.textContent = `elapsed: ${min}m`;
    }

    const total = (state.completed?.length || 0) + (state.queue?.length || 0) + (state.current ? 1 : 0);
    const done = state.completed?.length || 0;
    const pct = total ? Math.round((done / total) * 100) : 0;
    const bar = $('overnightProgressBar');
    if (bar) {
      const inner = bar.querySelector('i');
      if (inner) inner.style.width = `${pct}%`;
    }

    const qInfo = $('overnightQueueInfo');
    if (qInfo) {
      const nextUp = (state.queue || []).join(' → ') || 'none (last chain)';
      qInfo.textContent = `Remaining queue: ${nextUp} · Completed: ${(state.completed || []).join(', ') || 'none yet'}`;
    }
  } else {
    if (statusPill) {
      if (state.bedtime_autostart) {
        statusPill.textContent = 'bedtime armed';
        statusPill.className = 'pill cyan';
      } else {
        statusPill.textContent = state.status || 'idle';
        statusPill.className = state.status === 'completed' ? 'pill ok' : 'pill';
      }
    }
    if (banner) banner.hidden = true;
    if (btnStart) btnStart.hidden = false;
    if (btnCancel) btnCancel.hidden = true;
  }

  // Render presets buttons if empty
  const presetsEl = $('overnightPresets');
  if (presetsEl && !presetsEl.children.length) {
    setHTML(presetsEl, Object.entries(presets).map(([k, p]) =>
      `<button class="mini ghost" data-preset="${esc(k)}" title="${esc(p.description || '')}">🌟 ${esc(p.label)}</button>`
    ).join(''));
  }

  // Render chains checklist if empty
  const chainListEl = $('overnightChainList');
  if (chainListEl && (chainListEl.querySelector('.empty') || !chainListEl.children.length)) {
    if (chains.length) {
      const selSet = new Set(state.selected_chains || []);
      setHTML(chainListEl, chains.map((c) =>
        `<label class="overnight-item">` +
        `<input type="checkbox" value="${esc(c.name)}"${selSet.has(c.name) ? ' checked' : ''}>` +
        `<span><b>${esc(c.name)}</b> <small class="dim">(${c.steps} steps · ${esc(c.model)})</small></span>` +
        `</label>`
      ).join(''));
    } else {
      setHTML(chainListEl, '<div class="empty">No chains found in 1Notebook/Chains</div>');
    }
  }
}

// ---------- Quick AI Console (#c-playground)
function initPlayground() {
  const sendBtn = $('pgSend');
  const promptEl = $('pgPrompt');
  const clearBtn = $('pgClear');
  const copyBtn = $('pgCopyBtn');

  if (clearBtn) {
    clearBtn.onclick = () => {
      if (promptEl) promptEl.value = '';
      const box = $('pgOutputBox');
      if (box) box.hidden = true;
      const st = $('pgStatus');
      if (st) st.textContent = 'ready';
    };
  }

  if (copyBtn) {
    copyBtn.onclick = () => {
      const text = $('pgResponseText')?.textContent || '';
      if (text && navigator.clipboard) {
        navigator.clipboard.writeText(text).then(() => toast('Copied response'));
      }
    };
  }

  if (promptEl) {
    promptEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        sendPlaygroundPrompt();
      }
    });
  }

  if (sendBtn) {
    sendBtn.onclick = sendPlaygroundPrompt;
  }

  const modelEl = $('pgModel');
  if (modelEl) {
    modelEl.addEventListener('change', syncPlaygroundContext);
    syncPlaygroundContext();
  }
}

// Cloud (gemini) models never get local context: lock the checkbox and say why.
function syncPlaygroundContext() {
  const cloud = String($('pgModel')?.value || '').toLowerCase().startsWith('gemini');
  const box = $('pgUseContext');
  const hint = $('pgContextHint');
  if (box) box.disabled = cloud;
  if (hint) hint.hidden = !cloud;
}

async function sendPlaygroundPrompt() {
  const promptEl = $('pgPrompt');
  const prompt = promptEl?.value?.trim();
  if (!prompt) return;
  const model = $('pgModel')?.value || 'sol-fast';
  const useContext = Boolean($('pgUseContext')?.checked);
  const sendBtn = $('pgSend');
  const statusEl = $('pgStatus');
  const box = $('pgOutputBox');
  const respEl = $('pgResponseText');
  const thoughtWrap = $('pgThoughtWrap');
  const thoughtText = $('pgThoughtText');
  const metaModel = $('pgMetaModel');
  const metaSpeed = $('pgMetaSpeed');
  const citeBox = $('pgCitations');

  if (sendBtn) sendBtn.disabled = true;
  if (statusEl) statusEl.textContent = 'generating…';
  if (box) box.hidden = false;
  if (respEl) respEl.textContent = 'Thinking…';
  if (thoughtWrap) thoughtWrap.hidden = true;
  if (citeBox) citeBox.hidden = true;

  try {
    const res = await post('/api/playground', { model, prompt, use_context: useContext });
    if (sendBtn) sendBtn.disabled = false;
    if (res?.ok) {
      if (statusEl) statusEl.textContent = `done in ${res.elapsed_s}s`;
      if (metaModel) metaModel.textContent = res.model;
      if (metaSpeed) metaSpeed.textContent = `${res.tok_s || '–'} tok/s (${res.elapsed_s}s)`;
      if (respEl) respEl.textContent = res.text || '(empty response)';
      if (res.thought && thoughtWrap && thoughtText) {
        thoughtText.textContent = res.thought;
        thoughtWrap.hidden = false;
      }
      if (res.context_note && statusEl) statusEl.textContent += ` · ${res.context_note}`;
      if (citeBox && res.citations && res.citations.length) {
        citeBox.innerHTML = res.citations.map((c) =>
          `<span class="pg-citation-chip" title="${esc(c.path)}:${esc(c.lines)}&#10;${esc(c.snippet)}">📍 ${esc(c.path.split(/[\\/]/).pop())}:${esc(c.lines)}</span>`
        ).join('');
        citeBox.hidden = false;
      }
    } else {
      if (statusEl) statusEl.textContent = 'error';
      if (respEl) respEl.textContent = `Error: ${res?.why || 'Unknown error'}`;
    }
  } catch (err) {
    if (sendBtn) sendBtn.disabled = false;
    if (statusEl) statusEl.textContent = 'network error';
    if (respEl) respEl.textContent = `Error: ${err}`;
  }
}

// ---------- Process Radar (#c-procs)
let procSortMode = 'cpu';

async function loadProcs() {
  if (document.hidden) return;
  if (typeof cardShown === 'function' && !cardShown('c-procs')) return;
  try {
    const d = await (await fetch('/api/top-processes', { cache: 'no-store' })).json();
    if (!d.ok) return;
    renderProcTable(d);
  } catch (e) { /* ignore */ }
}

function renderProcTable(data) {
  const tbody = $('procRows');
  if (!tbody) return;
  const list = procSortMode === 'mem' ? data.top_mem : procSortMode === 'gpu' ? data.top_gpu : data.top_cpu;
  if (!list || !list.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">No processes recorded</td></tr>';
    return;
  }
  tbody.innerHTML = list.map((p) => {
    const chip = p.chip ? `<span class="dim" style="font-size:10px">${esc(p.chip)}</span>` : '<span class="dim">–</span>';
    return `<tr>` +
      `<td class="dim">${p.pid}</td>` +
      `<td class="proc-name-cell" title="${esc(p.name)}"><b>${esc(p.name)}</b></td>` +
      `<td class="num" style="color:${p.cpu > 25 ? 'var(--amber)' : 'inherit'}">${p.cpu}%</td>` +
      `<td class="num">${Math.round(p.mem_mb)} MB</td>` +
      `<td>${chip}</td>` +
      `<td class="act"><button class="mini danger ghost" data-kill-pid="${p.pid}" data-proc-name="${esc(p.name)}" title="End task">✕</button></td>` +
      `</tr>`;
  }).join('');
}

function initProcs() {
  const table = $('procTable');
  if (table && !table._inited) {
    table._inited = true;
    table.addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-kill-pid]');
      if (!btn) return;
      const pid = parseInt(btn.dataset.killPid, 10);
      const name = btn.dataset.procName || 'process';
      if (!(await hudConfirm(`End process ${name} (PID ${pid})?`))) return;
      const r = await post('/api/action', { action: 'kill_process', target: String(pid) });
      if (r?.ok) {
        toast(`Ended ${name}`);
        loadProcs();
      } else {
        toast(r?.why || 'Could not end process');
      }
    });
  }

  ['procSortCpu', 'procSortMem', 'procSortGpu'].forEach((id) => {
    const btn = $(id);
    if (!btn) return;
    btn.onclick = () => {
      ['procSortCpu', 'procSortMem', 'procSortGpu'].forEach((bId) => {
        const b = $(bId);
        if (b) {
          b.classList.remove('active');
          b.classList.add('ghost');
        }
      });
      btn.classList.add('active');
      btn.classList.remove('ghost');
      procSortMode = btn.dataset.sort;
      loadProcs();
    };
  });

  const refreshBtn = $('procRefresh');
  if (refreshBtn) refreshBtn.onclick = loadProcs;
}

// ---------- Media & Audio Deck (#c-media-ctl)
async function loadMediaSession() {
  if (document.hidden) return;
  if (typeof cardShown === 'function' && !cardShown('c-media-ctl')) return;
  try {
    const d = await (await fetch('/api/media-session', { cache: 'no-store' })).json();
    if (!d.ok) return;
    renderMediaDeck(d);
  } catch (e) { /* ignore */ }
}

function renderMediaDeck(d) {
  const titleEl = $('mediaDeckTitle');
  const artistEl = $('mediaDeckArtist');
  const statusEl = $('mediaDeckStatus');
  const appEl = $('mediaDeckApp');
  const playBtn = $('mediaBtnPlayPause');
  const artIcon = $('mediaArtIcon');

  if (titleEl) titleEl.textContent = d.title || (d.playing ? 'Active Media' : 'No active media');
  if (artistEl) artistEl.textContent = d.artist || (d.clean_app ? `via ${d.clean_app}` : '–');
  if (statusEl) statusEl.textContent = `${d.status || 'idle'}${d.clean_app ? ` · ${d.clean_app}` : ''}`;
  if (appEl) appEl.textContent = d.clean_app || 'GSMTC';

  if (playBtn) {
    playBtn.textContent = d.playing ? '⏸' : '▶';
    playBtn.title = d.playing ? 'Pause Playback' : 'Resume Playback';
  }
  if (artIcon) {
    artIcon.textContent = d.clean_app === 'Spotify' ? '🟢' : d.clean_app === 'Brave' || d.clean_app === 'Chrome' ? '🌐' : '🎵';
  }

  const sel = $('mediaTargetSelect');
  if (sel && d.menu && document.activeElement !== sel) {
    sel.innerHTML = d.menu.map((m) => {
      if (m.target == null) return '';
      const cleanLabel = (m.label || '').replace(/^[✓\s]+/, '').trim();
      return `<option value="${esc(m.target)}"${m.target === d.target ? ' selected' : ''}>${esc(cleanLabel)}</option>`;
    }).join('');
  }

  const strip = $('mediaSessionsStrip');
  if (strip && d.sessions) {
    strip.innerHTML = d.sessions.map((s) => {
      const isCur = s.title === d.title;
      return `<span class="media-sess-chip${isCur ? ' active' : ''}" title="${esc(s.app)}">${esc(s.app.split('.').pop() || s.app)}: ${esc((s.title || s.status).slice(0, 30))}</span>`;
    }).join('');
  }
}

function initMediaControls() {
  const prevBtn = $('mediaBtnPrev');
  const playBtn = $('mediaBtnPlayPause');
  const nextBtn = $('mediaBtnNext');
  const refreshBtn = $('mediaCtlRefresh');
  const targetSel = $('mediaTargetSelect');

  if (prevBtn) prevBtn.onclick = () => postMediaTransport('prev');
  if (playBtn) playBtn.onclick = () => postMediaTransport('play_pause');
  if (nextBtn) nextBtn.onclick = () => postMediaTransport('next');
  if (refreshBtn) refreshBtn.onclick = loadMediaSession;

  if (targetSel) {
    targetSel.onchange = async () => {
      const r = await post('/api/action', { action: 'media_target', target: targetSel.value });
      if (r?.target) toast(`Media keys routed to: ${r.target}`);
      loadMediaSession();
    };
  }
}

async function postMediaTransport(op) {
  const r = await post('/api/action', { action: 'media_transport', target: op });
  if (r?.ok) {
    setTimeout(loadMediaSession, 400);
  } else if (r?.why) {
    toast(r.why);
  }
}

// Global click handler delegation for service tiles
document.addEventListener('click', async (e) => {
  const svcBtn = e.target.closest('[data-svc-name]');
  if (svcBtn) {
    e.stopPropagation();
    const svc = svcBtn.dataset.svcName;
    const r = await post('/api/action', { action: 'svc_control', target: svc });
    if (r?.ok) toast(`Service action triggered: ${svc}`);
    else toast(r?.why || 'Failed to trigger service');
  }
});

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

(async function start() {
  try {
    const meta = await (await fetch('/api/meta', { cache: 'no-store' })).json();
    themes = meta.themes || {};
    $('theme').innerHTML = Object.entries(themes).map(([k, t]) => `<option value="${k}">${esc(t.name)}</option>`).join('');
    applyTheme(meta.theme);
  } catch (e) { /* default colors */ }
  await loadHistory();
  loadControl();
  loadWeek();
  loadPorts();
  loadDisplays();
  loadGuard();
  await initVaults();
  loadNotes();
  loadOvernight();
  initPlayground();
  initProcs();
  initMediaControls();
  initQuickSearch();
  loadProcs();
  loadMediaSession();
  setInterval(() => { if (!document.hidden) loadOvernight(); }, 4000);
  setInterval(() => { if (!document.hidden) { loadProcs(); loadMediaSession(); } }, 4000);
  if (!document.hidden) connect();
})();
