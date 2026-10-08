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
  const t = document.createElement('div'); t.className = 'toast'; t.textContent = text; document.body.append(t);
  setTimeout(() => t.remove(), 3200);
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
function drawChart(cv) {
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
  g.fillStyle = css('--dim'); g.font = '10px Segoe UI'; g.fillText({ '1h': '60 min', '24h': '24 h', '7d': '7 days' }[range], 4, h - 4); g.fillText('now', w - 22, h - 4);
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
  const tb = $('tickerBtn'); tb.hidden = false; tb.textContent = p.views.ticker ? 'Hide ticker' : 'Show ticker';
  tb.onclick = async () => { tb.textContent = '…'; await post('/api/views', { ticker: !p.views.ticker }); };
  $('foot').textContent = `pace ${p.views.pace_s}s · ${s.gpu_name || ''}`;
  const ls = $('loginStart'); if (!ls._busy) ls.checked = !!p.views.start_at_login;
  const no = $('notifyOn'); if (!no._busy) no.checked = p.views.notify !== false;

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

  // stability
  const sys = slide(p, 'SYS'); setHTML($('sysSeg'), sys ? segHtml(sys.segments) : '');
  const crashes = s.unexpected_reboots + s.gpu_resets, cc = $('crashCount');
  cc.textContent = crashes ? `${crashes}: Away blocked` : '0: Away allowed'; cc.style.color = crashes ? 'var(--red)' : 'var(--green)';
  const st = p.stability || {};
  $('ackUntil').textContent = st.since ? st.since.replace('T', ' ') : '–';
  $('ackNote').textContent = st.acknowledged || '';
  const w = p.wsl || {};
  setHTML($('wslState'), w.available === false ? '<span class="dim">unknown</span>' :
    `${esc(w.state || '?')}${w.failed?.length ? ` <span style="color:var(--red)">· ${w.failed.length} failed unit(s)</span>` : ''}`);

  // services
  const ports = { Router: ':11440', Embed: ':11443', HUD: ':7900', Chains: 'runner', WSL: 'running' };   // WSL off is normal: its services start on demand
  // the router's model process: which chip it's on (the AI should only ever be on the AMD card)
  const llama = ((p.gpu || {}).processes || []).filter((x) => /llama-server/i.test(x.name) && x.dedicated_gb + x.shared_gb >= 0.5);
  const aiChip = llama.map((x) => chipOf(p, x.pid)).find(Boolean);
  setHTML($('svcList'), Object.keys(ports).map((n) => {
    const up = s.services?.[n];
    return `<div class="${up ? 'up' : n === 'WSL' ? '' : 'down'}"><i></i>${n}<small>${up ? ports[n] : n === 'WSL' ? 'off' : 'down'}${n === 'Router' && up ? chipTag(aiChip, true) : ''}</small></div>`;
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
  addPoint(p.point); drawCharts();
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
  pill.textContent = { WONT_FIT: "WON'T FIT", EVICTED: 'SPILLED' }[verdict] || verdict || '…';
  pill.title = { WONT_FIT: 'the default model would not fit next to what is on the card now', EVICTED: 'part of the model is in system RAM (slow)',
                 TIGHT: 'little room left', OK: 'room for the model' }[verdict] || '';
  pill.className = `pill ${verdict === 'EVICTED' ? 'bad' : verdict === 'OK' ? 'ok' : 'warn'}`;
  const card = v.card_gb || s.vram_total_gb || 16, parts = [];
  if (v.available) {
    if (v.ai?.dedicated_gb >= 0.05) parts.push([`AI ${(v.ai.models || []).join(', ') || 'model'}`, v.ai.dedicated_gb, 'var(--green)']);
    const tints = ['var(--cyan)', 'var(--amber)', 'var(--dim)', 'var(--muted)'];
    (v.top_consumers || []).filter((t) => !/llama/i.test(t.name)).slice(0, 4).forEach((t, i) => parts.push([t.label, t.gb, tints[i]]));
  } else (s.vram_processes || []).slice(0, 4).forEach((t, i) => parts.push([t.name, t.dedicated_gb, ['var(--green)', 'var(--cyan)', 'var(--amber)', 'var(--dim)'][i]]));
  const known = parts.reduce((a, b) => a + b[1], 0), used = s.vram_used_gb || 0;
  if (used > known + 0.05) parts.push(['other', used - known, 'color-mix(in srgb, var(--dim) 50%, transparent)']);
  parts.push(['free', Math.max(0, card - Math.max(used, known)), 'transparent']);
  const stack = $('vramStack'); stack.classList.toggle('evicted', verdict === 'EVICTED');
  while (stack.children.length < parts.length) stack.append(document.createElement('i'));
  while (stack.children.length > parts.length) stack.lastChild.remove();
  parts.forEach(([n, gb, c], i) => { const el = stack.children[i]; el.style.flexGrow = Math.max(gb, 0.001); el.style.background = c; el.title = `${n}: ${gb.toFixed(1)} GB`; });
  setHTML($('vramLegend'), parts.filter((x) => x[0] !== 'free').map(([n, gb, c]) => `<span style="--c:${c}">${esc(n)} ${gb.toFixed(1)}</span>`).join(''));
  const notes = [];
  if (v.need?.model) notes.push(`${v.need.model} needs ~${v.need.gb} GB${v.spare_gb != null ? ` · ${v.spare_gb.toFixed(1)} GB spare` : ''}`);
  if (v.evicted) notes.push('Windows pushed part of the model out to system RAM: it runs slow until VRAM frees up');
  if (v.suggest_free?.length && verdict !== 'OK') notes.push(`free up: ${v.suggest_free.map((t) => `${t.label} ${t.gb} GB`).join(', ')}`);
  $('vramNote').textContent = notes.join(' · ');

  const h = p.heal || {};                       // the watcher's spill auto-heal (OBVLT tools/sol-llm-watch.ps1)
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
  if (v.used_gb != null && v.used_gb >= CARD_LIMIT_GB - 0.7) {
    why = `the card is nearly full (${v.used_gb.toFixed(1)} GB in use; Windows lets programs use about ${CARD_LIMIT_GB} GB)`
      + (v.others_gb != null ? `, and other apps hold ${v.others_gb.toFixed(1)} GB of it${hogs ? ` (${hogs})` : ''}` : '')
      + '. Closing some, or moving them to the Intel chip in Settings, makes room.';
  } else {
    why = `the card has room now (${v.used_gb != null ? v.used_gb.toFixed(1) + ' GB in use' : 'no reading'}), so the slow answer came earlier, from a long prompt, or from another program using the GPU.`;
  }
  note.textContent = `Slow: ${why}`;
  note.hidden = false;
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
  // waiting chains get "Run now": one run that skips the polite waits (the runner still stops for the game guard,
  // an Away queue job, or an Away-only model)
  const pend = c.pending || [];
  const skip = (name) => `<button class="mini go" data-chain="${esc(name)}" data-op="now" title="Skip the wait: run it now">⏭ Run now</button>`;
  const held = run && run.waiting ? `<div class="item"><span>${esc(run.chain)} <span class="dim">(running)</span></span><span style="color:var(--amber)">${esc(run.waiting)}${skip(run.chain)}</span></div>` : '';
  setHTML($('chPending'), held + pend.map((x) => `<div class="item"><span>${esc(x.chain)}</span><span style="color:${x.blocked ? 'var(--amber)' : 'var(--cyan)'}">${esc(x.blocked || 'next')}${x.blocked ? skip(x.chain) : ''}</span></div>`).join('')
    || '<div class="empty">nothing waiting</div>');
  const sch = c.scheduled || [];
  setHTML($('chSched'), sch.length ? sch.map((x) => `<div class="item"><span>${esc(x.chain)}</span><span>${esc(nextLabel(x.next))}</span></div>`).join('')
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

// ---------- the stream (only while this tab is visible)
// the Exam assist card (exam_review.py): solves practice exam questions in real time
const REV_PILL = { watching: 'ok', reading: '', answering: '', waiting: 'warn', paused: 'warn', error: 'bad', stopped: '' };
function renderReview(p) {
  const r = p.review; if (!r) return;
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
  const snipBtn = $('revSnipBtn'); if (snipBtn) { snipBtn.textContent = r.parts_count > 1 ? `➕ Add Snip (${r.parts_count} parts)` : '➕ Add Snip (Scroll)'; }
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
      const mark = (lb) => (c.answer_labels || c.correct_labels || []).includes(lb) ? 'right' : (c.your_labels || []).includes(lb) ? 'mine' : '';
      contentHtml = (c.choices || []).map((o) => `<div class="rev-opt ${mark(o.label)}"><b>${esc(o.label)}.</b> ${esc(o.text)}` +
        `${(c.your_labels || []).includes(o.label) ? ' <span class="dim small">your selection</span>' : ''}` +
        `${(c.answer_labels || c.correct_labels || []).includes(o.label) ? ` <span class="small" style="color:${isGemini ? 'var(--cyan)' : 'var(--green)'}">✓ answer</span>` : ''}</div>`).join('');
    }

    const exhibitHtml = c.exhibit_text ? `<details style="margin:6px 0;font-size:0.85rem"><summary class="dim" style="cursor:pointer">📊 Exhibit / Diagram</summary><pre style="background:var(--card);padding:6px;border-radius:4px;overflow-x:auto">${esc(c.exhibit_text)}</pre></details>` : '';
    const typeLabel = qType === 'matching' ? 'Matching' : qType === 'fill_in_the_blank' ? 'Fill-in-Blank' : qType === 'ordering' ? 'Ordering' : '';
    const typeBadge = typeLabel ? ` <span class="pill" style="font-size:0.75rem;padding:2px 6px">${typeLabel}</span>` : '';
    const modelBadge = isGemini ? ' <span class="pill" style="background:#0284c7;color:#fff;font-size:0.75rem;padding:2px 6px">✨ Gemini 3.8 Flash</span>' : '';
    const head = `<span style="color:${isGemini ? 'var(--cyan)' : 'var(--green)'}">💡 Answer: ${esc(c.answer || (c.answer_labels || []).join(', '))} · ${esc(c.topic || 'Solved')}</span>${typeBadge}${modelBadge}`;
    const whyWrong = c.why_previous_wrong ? `<p class="rev-explain" style="color:var(--amber);margin-bottom:6px"><b>Previous attempt issue:</b> ${esc(c.why_previous_wrong)}</p>` : '';
    const body = `${whyWrong}<p class="rev-explain">${esc(c.explanation)}</p>`;
    const retryBtn = isGemini ? '' : `<button class="mini danger" style="margin-top:8px" data-act="review" data-target="retry_gemini" title="Verify or re-solve with Gemini 3.8 Flash">🔍 Verify / Retry (Gemini)</button>`;
    setHTML(cur, `<div class="row"><b>${head}</b><span class="dim small">${esc(c.at || '')}${c.seconds ? ` · ${c.seconds} s` : ''}</span></div>` +
      `<div class="rev-q">${esc(c.question)}</div>${exhibitHtml}${contentHtml}${body}${retryBtn}`);
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
    if (b.dataset.confirm && !confirm(b.dataset.confirm)) return;
    const r = await post('/api/action', { action: b.dataset.act, target: b.dataset.target || '' });
    if (r.why) toast(r.why);
    return;
  }
  const card = e.target.closest('.card[data-open]');
  if (card && !e.target.closest('button, a, select')) post('/api/action', { action: 'open', target: card.dataset.open });
});
$('stopAi').onclick = async () => {
  if (!confirm('Stop the Away work now? The running job goes back in the queue (finished work is kept).')) return;
  const r = await post('/api/action', { action: 'stop_ai' }); toast(r.why || 'stopping');
};
$('notifyOn').onchange = async (e) => {
  const box = e.target; box._busy = true;
  const r = await post('/api/action', { action: 'notify', target: box.checked ? 'on' : 'off' });
  box.checked = !!r.notify; box._busy = false; if (r.why) toast(r.why);
};
$('loginStart').onchange = async (e) => {
  const box = e.target; box._busy = true;
  const r = await post('/api/action', { action: 'login_start', target: box.checked ? 'on' : 'off' });
  box.checked = !!r.start_at_login; box._busy = false; if (r.why) toast(r.why);
};
$('theme').onchange = (e) => { applyTheme(e.target.value); post('/api/action', { action: 'theme', target: e.target.value }); };

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
const OP_LABEL = { run: 'Run now', now: '⏭ Run now', pause: 'Pause', resume: 'Resume', cancel: 'Cancel' };
let control = null;
async function loadControl() {
  if (document.hidden) return;
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
  if (schedEdit) return;                     // don't redraw under an open schedule editor
  setHTML($('chainList'), control.chains.map((c) => {
    const ops = OPS[c.status] || ['run', 'pause'];
    const meta = [c.schedule && `⏱ ${c.schedule}`, c.watch && 'watches a folder', c.model].filter(Boolean).join(' · ');
    return `<div class="crow"><span>${esc(c.name)}</span><span class="status ${esc(c.status)}">${esc(c.status)}</span>` +
      `<span class="meta" title="${esc(meta)}">${esc(meta)}</span><span class="acts">` +
      `<button class="mini ghost" data-sched="${esc(c.name)}" title="Change when it runs">⏱</button>` +
      ops.map((o) => `<button class="mini" data-chain="${esc(c.name)}" data-op="${o}"${o === 'cancel' ? ' data-confirm="Cancel this chain? It stops after its current step; finished steps are kept."' : ''}>${OP_LABEL[o]}</button>`).join('') +
      (c.result_time ? `<button class="mini ghost" data-result="${esc(c.name)}" title="result from ${esc(c.result_time)}">Result</button>` : '') + '</span></div>';
  }).join('') || '<div class="empty">no chain notes in 1Notebook\\Chains</div>');
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
  m = t.match(/^weekly ([a-z, ]+?) (\d{1,2}):(\d{2})$/);
  if (m) return { kind: 'weekly', time: `${m[2].padStart(2, '0')}:${m[3]}`, days: m[1].split(/[ ,]+/).map((d) => d.slice(0, 3)) };
  return { kind: 'daily', time: '07:00', days: [] };
}
function schedEditor(c) {
  const s = schedParts(c.schedule);
  const days = DAY_NAMES.map((d) => `<label><input type="checkbox" value="${d}"${s.days.includes(d.toLowerCase()) ? ' checked' : ''}>${d}</label>`).join('');
  return `<div class="sched-edit" data-for="${esc(c.name)}"><b>${esc(c.name)}</b> runs ` +
    `<select class="sk"><option value=""${s.kind === '' ? ' selected' : ''}>never by itself</option><option value="daily"${s.kind === 'daily' ? ' selected' : ''}>every day</option>` +
    `<option value="weekly"${s.kind === 'weekly' ? ' selected' : ''}>on these days</option><option value="away"${s.kind === 'away' ? ' selected' : ''}>when Away starts</option></select>` +
    `<span class="sdays"${s.kind === 'weekly' ? '' : ' hidden'}>${days}</span>` +
    `<span class="stime"${s.kind === 'daily' || s.kind === 'weekly' ? '' : ' hidden'}>at <input type="time" class="st" value="${s.time}"></span>` +
    `<button class="mini go" data-sched-save>Save</button><button class="mini ghost" data-sched-cancel>Cancel</button>` +
    (c.status === 'paused' ? '<span class="dim small">paused: Resume it for the schedule to apply</span>' : '') + '</div>';
}
$('chainList').addEventListener('click', async (e) => {
  const open = e.target.closest('[data-sched]');
  if (open) {
    e.stopPropagation();
    const c = (control?.chains || []).find((x) => x.name === open.dataset.sched);
    document.querySelectorAll('.sched-edit').forEach((el) => el.remove());
    if (!c || schedEdit === c.name) { schedEdit = null; return; }
    schedEdit = c.name;
    open.closest('.crow').insertAdjacentHTML('afterend', schedEditor(c));
    return;
  }
  const box = e.target.closest('.sched-edit');
  if (!box) return;
  if (e.target.closest('[data-sched-cancel]')) { e.stopPropagation(); box.remove(); schedEdit = null; return; }
  if (!e.target.closest('[data-sched-save]')) return;
  e.stopPropagation();
  const kind = box.querySelector('.sk').value, time = box.querySelector('.st').value || '07:00';
  const days = [...box.querySelectorAll('.sdays input:checked')].map((i) => i.value);
  if (kind === 'weekly' && !days.length) { toast('pick at least one day'); return; }
  const schedule = kind === 'daily' ? `daily ${time}` : kind === 'weekly' ? `weekly ${days.join(',')} ${time}` : kind === 'away' ? 'on away' : '';
  const r = await post('/api/action', { action: 'chain', target: box.dataset.for, op: 'schedule', schedule });
  toast(r.why || (r.ok ? 'saved' : 'that did not work'));
  if (r.ok) { box.remove(); schedEdit = null; loadControl(); }
}, true);
$('chainList').addEventListener('change', (e) => {
  const box = e.target.closest('.sched-edit');
  if (!box || !e.target.classList.contains('sk')) return;
  const k = e.target.value;
  box.querySelector('.sdays').hidden = k !== 'weekly';
  box.querySelector('.stime').hidden = !(k === 'daily' || k === 'weekly');
});
const pendingModels = new Map();   // model -> the state it had when you pressed its button (shown as "…" until it changes)
document.addEventListener('click', async (e) => {
  const t = e.target.closest('[data-model],[data-models],[data-power]');
  if (!t || t.disabled) return;
  e.stopPropagation();
  if (t.dataset.confirm && !confirm(t.dataset.confirm)) return;
  let r;
  if (t.dataset.model) {
    const cur = (payload?.models || []).find((m) => m.id === t.dataset.model);
    if (cur) pendingModels.set(cur.id, cur.state);
    r = await post('/api/action', { action: 'model', target: t.dataset.model, op: t.dataset.op });
  } else if (t.dataset.models) r = await post('/api/action', { action: 'models_unload_all' });
  else {
    if (t.dataset.power === 'off' && !confirm('Turn the local AI off? Every model unloads and nothing loads until you turn it on again (chains and asks wait).')) return;
    r = await post('/api/action', { action: 'ai_power', target: t.dataset.power });
  }
  if (r?.why) toast(r.why);
  if (r && !r.ok && t.dataset.model) pendingModels.delete(t.dataset.model);
}, true);document.addEventListener('click', async (e) => {
  const t = e.target.closest('[data-chain],[data-remove],[data-answer],[data-result]');
  if (!t) return;
  e.stopPropagation();
  if (t.dataset.confirm && !confirm(t.dataset.confirm)) return;
  let r = null;
  if (t.dataset.chain) r = await post('/api/action', { action: 'chain', target: t.dataset.chain, op: t.dataset.op });
  else if (t.dataset.remove) r = await post('/api/action', { action: 'ask_remove', target: t.dataset.remove });
  else if (t.dataset.answer) { const a = await (await fetch(`/api/answer?file=${encodeURIComponent(t.dataset.answer)}`)).json(); if (a.ok) openReader(t.dataset.answer.replace(/\.md$/, ''), a.text); else toast(a.why); return; }
  else if (t.dataset.result) { const a = await (await fetch(`/api/chain-result?name=${encodeURIComponent(t.dataset.result)}`)).json(); if (a.ok) openReader(`${t.dataset.result}: latest result`, a.text); else toast(a.why); return; }
  if (r?.why) toast(r.why);
  loadControl();
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
  ].join(''));
  const disks = Object.entries(w.disks || {}).map(([d, g]) => `<span style="color:${g <= -5 ? 'var(--red)' : g >= 5 ? 'var(--green)' : 'var(--dim)'}">${esc(d)}: ${g > 0 ? '+' : ''}${g} GB</span>`).join(' · ');
  setHTML($('weekDisks'), `Chains: ${esc(chains)}${disks ? ' · Disks: ' + disks : ''}`);
  $('weekCover').textContent = w.covered_hours < 24 * w.days ? `from ${w.covered_hours} h of history so far` : `last ${w.days} days`;
}
setInterval(loadWeek, 300000);

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
    allNotesCache = notes;
    $('notesVaultCount').textContent = `${notes.length} recent in ${selectedVault}`;
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

// Quick Model Switcher
document.addEventListener('click', (e) => {
  const qm = e.target.closest('[data-quick-model]');
  if (qm) {
    const model = qm.dataset.quickModel;
    post('/api/action', { action: 'model', target: model, op: 'load' }).then((r) => toast(r.why));
  }
});

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
  }
});

function getPaletteItems(query) {
  const q = query.toLowerCase().trim();
  const items = [
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
  if (!confirm(`Close :${b.dataset.port}?\n\n${what}\n\nIt is asked to stop first, then forced if it ignores that. Anything unsaved in it is lost.`)) return;
  b.disabled = true;
  const r = await post('/api/action', { action: 'close_port', target: b.dataset.port, pid: Number(b.dataset.pid) });
  toast(r.why || (r.ok ? 'closed' : 'could not close it'));
  loadPorts();
});
setInterval(() => { if (!document.hidden) loadPorts(); }, 5000);

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
  await initVaults();
  loadNotes();
  if (!document.hidden) connect();
})();
