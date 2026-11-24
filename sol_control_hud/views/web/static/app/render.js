// render one pushed message (split from app.js)
'use strict';
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
  const st = p.stability || {};
  const crashes = s.unexpected_reboots + s.gpu_resets;
  const totalCrashes = Math.max(crashes, st.crash_watch_new || 0);
  const cc = $('crashCount');
  cc.textContent = totalCrashes ? `${totalCrashes}: Away blocked` : '0: Away allowed';
  cc.style.color = totalCrashes ? 'var(--red)' : 'var(--green)';
  const ackBtn = $('ackCrashBtn');
  if (ackBtn) {
    ackBtn.hidden = false;
    ackBtn.className = totalCrashes ? 'mini danger' : 'mini ghost';
  }
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
