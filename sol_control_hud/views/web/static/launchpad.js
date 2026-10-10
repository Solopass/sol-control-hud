// Projects and Media APIs cards: one click to open, start or stop what lives in D:\Workspace.
// Loaded after app.js and uses its helpers ($, esc, post, toast, setHTML, upWords).
// The page sends a project or service *name*; the server looks up what that means (launchpad.yaml, launch.json).
// After a Start the cards poll every 2 s for a minute and open the page once its port answers.
'use strict';
let projAll = false, projCache = [], mediaCache = [], fastUntil = 0;
const pendingOpen = {};          // name -> started at (ms): open its page once it runs
const liveOpen = {};             // media name -> last Live answer
const OPEN_WAIT_MS = 300000;    // a start can include a build (OmniTools: ~1.5 min after its code changed)
const TAGS = ['active', 'paused', 'idea', 'finished', 'archived', 'deprecated'];
let menuFor = null;              // the project whose ⋯ menu is open

function ago(epoch) {
  if (!epoch) return '';
  const s = Date.now() / 1000 - epoch;
  if (s < 90) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 172800) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
function openUrl(url) { if (url) window.open(url, '_blank', 'noopener'); }
async function launch(kind, name, op) {
  const r = await post('/api/action', { action: kind, target: name, op });
  toast(r.why || (r.ok ? 'done' : 'that did not work'));
  if (r.ok && op === 'start') {
    if (r.url) openUrl(r.url);                       // it was already running
    else if (r.auto_open) pendingOpen[name] = Date.now();
  }
  if (op === 'start' || op === 'stop') { fastUntil = Date.now() + 60000; setTimeout(refreshLaunchpad, 1200); }
  return r;
}
function openWhenUp(name, running, url) {
  if (!pendingOpen[name]) return;
  if (Date.now() - pendingOpen[name] > OPEN_WAIT_MS) { delete pendingOpen[name]; return; }
  if (running && url) { delete pendingOpen[name]; openUrl(url); }
}

// ---------- Projects
function projDot(p) {
  const g = p.git || {};
  if (p.running) return 'run';
  if (g.dirty_files && (g.dirty_days || 0) > 3) return 'bad';
  if (g.dirty_files) return 'warn';
  return 'idle';
}
function projRow(p) {
  const g = p.git || {};
  const chips = [];
  if (p.status) chips.push(`<span class="pill ${p.status === 'paused' ? 'warn' : p.status === 'active' ? 'ok' : 'dim'}" title="${p.status_from === 'you' ? 'your tag' : 'from its README'}">${esc(p.status)}${p.status_from === 'you' ? ' ✎' : ''}</span>`);
  if (p.running) chips.push(`<span class="pill ok" title="${p.running.where === 'wsl' ? 'runs in WSL' : `pid ${p.running.pid}`}">:${p.running.port}${p.running.up_seconds ? ` · ${upWords(p.running.up_seconds)}` : ''}</span>`);
  else if (p.busy_by) chips.push(`<span class="pill warn" title="Something else holds its port">:${p.port} taken · ${esc(p.busy_by)}</span>`);
  if (g.dirty_files) chips.push(`<span class="pill ${(g.dirty_days || 0) > 3 ? 'red' : 'warn'}" title="Uncommitted changes; the oldest is ${g.dirty_days ?? '?'} days old">${g.dirty_files} uncommitted${g.dirty_days ? ` · ${g.dirty_days}d` : ''}</span>`);
  if (p.hidden) chips.push(`<span class="pill dim" title="${esc(p.why_hidden)}">hidden</span>`);
  const acts = [];
  if (p.running && p.url) acts.push(`<button class="mini" data-p="open" title="${esc(p.url)}">↗ Open</button>`);
  else if (p.open_file) acts.push(`<button class="mini" data-p="file" title="${esc(p.open_file)}">↗ Open</button>`);
  if (!p.running && p.can_start) acts.push(`<button class="mini go" data-p="start" title="${p.port ? `Start it on :${p.port}` : 'Start it'}">▶ Start</button>`);
  if (p.running && p.can_stop) acts.push(`<button class="mini danger" data-p="stop" title="Stop it">■</button>`);
  if (p.live) acts.push(`<button class="mini ghost" data-p="live" title="${esc(p.live)}">🌐 Site</button>`);
  if (p.github) acts.push(`<button class="mini ghost" data-p="github" title="${esc(p.github)}">GitHub</button>`);
  acts.push(`<button class="mini ghost" data-p="editor" title="Open in VS Code">‹/›</button>`);
  acts.push(`<button class="mini ghost" data-p="folder" title="Open the folder">📁</button>`);
  acts.push(`<button class="mini ghost${menuFor === p.name ? ' on' : ''}" data-p="menu" title="Tag or hide">⋯</button>`);
  const commit = g.last_commit ? `last commit ${ago(g.last_commit)}${g.subject ? ` · ${esc(g.subject)}` : ''}` : (p.git ? '' : 'not a git repo');
  return `<div class="jrow" data-name="${esc(p.name)}">`
    + `<i class="dot ${projDot(p)}"></i>`
    + `<div class="jmain"><div class="jtitle"><b>${esc(p.title)}</b>${p.title !== p.name ? `<span class="dim small">${esc(p.name)}</span>` : ''}${chips.join('')}</div>`
    + `<div class="meta" title="${esc(p.description)}">${esc(p.description || '')}</div>`
    + `<div class="meta small" title="${esc(g.subject || '')}">${commit}</div></div>`
    + `<div class="acts">${acts.join('')}</div>`
    + (menuFor === p.name ? projMenu(p) : '') + `</div>`;
}
function projMenu(p) {
  const mine = p.status_from === 'you' ? p.status : '';
  const tags = TAGS.map((t) => `<button class="mini ${mine === t ? 'on' : 'ghost'}" data-p="tag" data-tag="${t}">${t}</button>`).join('');
  return `<div class="jmenu"><span class="dim small">tag</span>${tags}`
    + (mine ? `<button class="mini ghost" data-p="tag" data-tag="" title="Use the README's status again">clear</button>` : '')
    + `<span class="sep"></span>`
    + (p.hidden ? `<button class="mini" data-p="show">show again</button>` : `<button class="mini ghost" data-p="hide" title="Fold it into + hidden">hide</button>`)
    + `<span class="dim small">archived / deprecated also hide it</span></div>`;
}
async function loadProjects() {
  if (typeof cardShown === 'function' && !cardShown('c-projects')) return;   // hidden in this layout: not polled (layouts.js)
  let d;
  try { d = await (await fetch('/api/projects', { cache: 'no-store' })).json(); } catch (e) { return; }
  if (!d.ok || !d.projects) return;
  projCache = d.projects;
  const q = ($('projSearch').value || '').toLowerCase().trim();
  const rows = d.projects.filter((p) => (projAll || !p.hidden)
    && (!q || `${p.name} ${p.title} ${p.description}`.toLowerCase().includes(q)));
  const c = d.counts || {};
  $('projSummary').textContent = `${c.shown} projects · ${c.running} running${c.dirty ? ` · ${c.dirty} with uncommitted changes` : ''}`;
  setHTML($('projList'), rows.length ? rows.map(projRow).join('') : '<div class="empty">no project matches</div>');
  setHTML($('projHidden'), `${projAll ? '–' : '+'} ${c.hidden} hidden`);
  for (const p of d.projects) openWhenUp(p.name, p.running, p.url);
}
$('projSearch').oninput = () => loadProjects();
$('projHidden').onclick = () => { projAll = !projAll; $('projHidden').setAttribute('aria-pressed', String(projAll)); loadProjects(); };
$('projRefresh').onclick = () => loadProjects();
$('projList').addEventListener('click', async (e) => {
  const b = e.target.closest('button[data-p]');
  if (!b) return;
  const name = b.closest('.jrow').dataset.name, p = projCache.find((x) => x.name === name) || {}, op = b.dataset.p;
  if (op === 'open') return openUrl(p.url);
  if (op === 'live') return openUrl(p.live);
  if (op === 'github') return openUrl(p.github);
  if (op === 'menu') { menuFor = menuFor === name ? null : name; return loadProjects(); }
  if (op === 'tag' || op === 'hide' || op === 'show') {
    const r = await post('/api/action', { action: 'project', target: name, op, tag: b.dataset.tag || '' });
    toast(r.why || (r.ok ? 'saved' : 'that did not work'));
    if (op !== 'tag' || (b.dataset.tag && ['archived', 'deprecated'].includes(b.dataset.tag))) menuFor = null;
    return loadProjects();
  }
  if (op === 'stop' && !confirm(`Stop ${p.title}?\n\nIt is asked to stop first, then forced if it ignores that. Anything unsaved in it is lost.`)) return;
  b.disabled = true;
  await launch('project', name, op);
  b.disabled = false;
});

// ---------- Media APIs
const STATE_CLASS = { up: 'run', busy: 'run', ready: 'ok', asleep: 'idle', off: 'idle', down: 'bad', busy_port: 'warn' };
const JOB_ICON = { finished: '✓', completed: '✓', done: '✓', failed: '✕', error: '✕', cancelled: '–', queued: '…', processing: '◔', running: '◔' };
function jobLine(j) {
  const st = String(j.status || '').toLowerCase();
  const icon = JOB_ICON[st] || '·';
  const cls = /fail|error/.test(st) ? 'bad' : /finish|complete|done/.test(st) ? 'ok' : 'run';
  const pct = j.progress != null && !/finish|complete|done|fail|cancel/.test(st) ? ` ${Math.round(j.progress)}%` : '';
  const sub = [j.what, j.detail].filter(Boolean).join(' · ');
  return `<div class="mjob" title="${esc(j.error || j.detail || j.status)}"><span class="jicon ${cls}">${icon}</span>`
    + `<span class="jt">${esc(j.title)}</span><span class="dim">${esc(sub)}${pct}</span>`
    + `<span class="dim">${j.at ? ago(j.at) : ''}</span></div>`;
}
function healthFacts(h) {
  const f = [['active', h.active_jobs ?? 0]];
  if (h.version) f.push(['version', h.version]);
  if (h.ytdlp_version) f.push(['yt-dlp', h.ytdlp_version]);
  if (h.idle_seconds != null) {
    const left = h.auto_close_enabled ? (h.idle_timeout_seconds || 0) - h.idle_seconds : 0;
    f.push(['idle', `${h.idle_seconds}s${left > 0 ? ` · closes in ${Math.round(left / 60)}m` : ''}`]);
  }
  return f.map(([k, v]) => `<span><span class="dim">${esc(k)}</span> ${esc(v)}</span>`).join('');
}
function weekLine(w) {
  const parts = Object.entries(w || {}).filter(([, n]) => n).map(([s, n]) => `${n} ${s}`);
  return parts.length ? `<div class="meta small">this week: ${esc(parts.join(' · '))}</div>` : '';
}
function mediaTile(t) {
  const facts = (t.facts || []).map(([k, v]) => `<span><span class="dim">${esc(k)}</span> ${esc(v)}</span>`).join('');
  const where = [t.port ? `:${t.port}` : '', t.runs_in === 'wsl' ? 'WSL' : 'Windows', t.up_seconds ? `up ${upWords(t.up_seconds)}` : ''].filter(Boolean).join(' · ');
  const up = t.state === 'up' || t.state === 'busy';
  let body = '';
  if ((t.recent || []).length) body += `<h3>Recent jobs</h3>${t.recent.slice(0, 3).map(jobLine).join('')}${weekLine(t.week)}`;
  if ((t.files || []).length) body += `<h3>Newest outputs</h3>${t.files.slice(0, 3).map((f) => jobLine({ title: f.title, status: 'done', what: `${f.size_mb} MB`, at: f.at })).join('')}`;
  if (t.latest_note) body += `<div class="meta small" title="${esc(t.latest_note.title)}">newest note: ${esc(t.latest_note.title)} · ${ago(t.latest_note.at)}</div>`;
  // E3: a job the file still calls queued/processing while the service isn't running (stuck), or silent for an hour
  const stuck = (t.stuck || []).map((s) => `<div class="${s.why === 'stuck' ? 'bad' : 'warn'}" title="${esc(s.status)} · last written ${s.updated ? new Date(s.updated * 1000).toLocaleString() : '?'}">`
    + `${s.why === 'stuck' ? '⚠ stuck' : '◔ no progress for ' + upWords(s.age_s)}: ${esc(s.title)}`
    + `${s.why === 'stuck' ? ` · says ${esc(s.status)} but ${esc(t.title)} isn't running` : ''}</div>`).join('');
  if (stuck) body = `<div class="mstuck">${stuck}</div>` + body;
  if (!body) body = `<div class="meta small">${t.project === 'omni-tools' ? 'works in the browser: nothing to report until it\'s open' : 'nothing yet'}</div>`;
  const live = liveOpen[t.name];
  if (live) {
    const inner = !live.ok ? `<div class="meta small">${esc(live.why)}</div>`
      : ((live.jobs || []).length ? live.jobs.map(jobLine).join('') : '<div class="meta small">its queue is empty</div>')
        + (live.health ? `<div class="mfacts">${healthFacts(live.health)}</div>` : '');
    body += `<div class="mlive"><h3>Live <span class="dim">${new Date(live.at * 1000).toLocaleTimeString()}</span></h3>${inner}</div>`;
  }
  const acts = [];
  if (up) acts.push(`<button class="mini" data-m="open">↗ Open</button>`);
  if (t.can_start) acts.push(`<button class="mini go" data-m="start" title="${t.state === 'ready' ? 'Its socket is waiting: this opens it, which starts it' : 'Start it'}">▶ Start</button>`);
  if (t.docs && up) acts.push(`<button class="mini ghost" data-m="docs" title="${esc(t.docs)}">API docs</button>`);
  if (t.can_live) acts.push(`<button class="mini ghost" data-m="live" title="Ask the running service for its job list">Live</button>`);
  if (t.has_folder) acts.push(`<button class="mini ghost" data-m="folder" title="Open its output folder">📁</button>`);
  if (t.can_stop) acts.push(`<button class="mini danger" data-m="stop" title="Shut it down (refused while a job runs)">■</button>`);
  const pill = t.state === 'busy' ? `working · ${t.active_jobs} job${t.active_jobs === 1 ? '' : 's'}` : t.state_words;
  const pillCls = t.state === 'down' ? 'red' : t.state === 'busy_port' ? 'warn' : up ? 'ok' : 'dim';
  return `<div class="mtile" data-name="${esc(t.name)}">`
    + `<div class="mhead"><i class="dot ${STATE_CLASS[t.state] || 'idle'}"></i><b>${esc(t.title)}</b><span class="pill ${pillCls}">${esc(pill)}</span></div>`
    + `<div class="meta">${esc(t.what)}</div><div class="meta small">${esc(where)}${t.note ? ` · ${esc(t.note)}` : ''}</div>`
    + (facts ? `<div class="mfacts">${facts}</div>` : '')
    + `<div class="mbody">${body}</div><div class="acts">${acts.join('')}</div></div>`;
}
async function loadMedia() {
  if (typeof cardShown === 'function' && !cardShown('c-media')) return;   // hidden in this layout: not polled (layouts.js)
  let d;
  try { d = await (await fetch('/api/media', { cache: 'no-store' })).json(); } catch (e) { return; }
  if (!d.ok || !d.tiles) return;
  mediaCache = d.tiles;
  const c = d.counts || {};
  $('mediaSummary').textContent = `${c.up} of ${c.total} running${d.wsl && d.wsl !== 'Running' ? ` · WSL ${String(d.wsl).toLowerCase()}` : ''}`;
  setHTML($('mediaTiles'), d.tiles.map(mediaTile).join(''));
  renderNotes(d.notes);
  for (const t of d.tiles) openWhenUp(t.project || t.name, t.state === 'up' || t.state === 'busy', t.url);
}
$('mediaRefresh').onclick = () => loadMedia();
$('mediaTiles').addEventListener('click', async (e) => {
  const b = e.target.closest('button[data-m]');
  if (!b) return;
  const name = b.closest('.mtile').dataset.name, t = mediaCache.find((x) => x.name === name) || {}, op = b.dataset.m;
  if (op === 'open') return openUrl(t.url);
  if (op === 'docs') return openUrl(t.docs);
  if (op === 'live') {
    b.disabled = true;
    try { liveOpen[name] = await (await fetch(`/api/media-live?name=${encodeURIComponent(name)}`, { cache: 'no-store' })).json(); } catch (err) { liveOpen[name] = { ok: false, why: 'no answer' }; }
    liveOpen[name].at = liveOpen[name].at || Date.now() / 1000;
    b.disabled = false;
    return loadMedia();
  }
  if (op === 'stop' && !confirm(`Stop ${t.title}?\n\nIt is refused while a job runs.${t.runs_in === 'wsl' && t.project !== 'omni-tools' ? ' Its socket stays, so it starts again the next time something uses it.' : ''}`)) return;
  b.disabled = true;
  await launch('media', name, op);
  b.disabled = false;
});

// ---------- recording or link -> transcript note (runs the transcript-note workflow; progress from the run database)
const STEP_WORDS = { transcribe: 'transcribing', parts: 'splitting it into parts', part_notes: 'taking notes', summary: 'merging the notes', note: 'writing the note' };
function noteRun(r) {
  const st = r.status;
  const cls = st === 'succeeded' ? 'ok' : /failed|cancelled|interrupted|needs_user/.test(st) ? 'bad' : 'run';
  const icon = cls === 'ok' ? '✓' : cls === 'bad' ? '✕' : '◔';
  const what = st === 'succeeded' ? (r.note ? `→ ${r.note}` : 'done')
    : cls === 'run' ? (r.step === 'part_notes' && r.item ? `notes on part ${String(r.item).replace('/', ' of ')}` : (STEP_WORDS[r.step] || st))
    : st.replace('_', ' ');
  const open = r.has_note ? `<button class="mini ghost" data-run="${r.id}">open note</button>` : '';
  return `<div class="mjob nrun" title="${esc(r.error || what)}"><span class="jicon ${cls}">${icon}</span>`
    + `<span class="jt">${esc(r.title)}</span><span class="dim">${esc(what)}</span><span class="dim">${ago(r.at)}</span>${open}</div>`;
}
function renderNotes(runs) {
  setHTML($('noteRuns'), (runs || []).length ? `<div class="meta small">recent</div>${runs.map(noteRun).join('')}` : '');
  if ((runs || []).some((r) => ['queued', 'running'].includes(r.status))) fastUntil = Math.max(fastUntil, Date.now() + 10000);
}
$('noteForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = $('noteForm').querySelector('button[type=submit]');
  btn.disabled = true;
  const r = await post('/api/action', { action: 'media', target: 'media-api', op: 'note', source: $('noteSource').value, title: $('noteTitle').value });
  btn.disabled = false;
  toast(r.why || (r.ok ? 'started' : 'that did not work'));
  if (r.ok) { $('noteSource').value = ''; $('noteTitle').value = ''; fastUntil = Date.now() + 60000; setTimeout(loadMedia, 1500); }
});
$('noteRuns').addEventListener('click', async (e) => {
  const b = e.target.closest('button[data-run]');
  if (!b) return;
  const r = await post('/api/action', { action: 'media', target: 'media-api', op: 'open_note', run: Number(b.dataset.run) });
  toast(r.why || (r.ok ? 'opened' : 'no note'));
});

// ---------- Solang Karaoke Studio Widget
const SOLANG_URL = 'http://127.0.0.1:3000';
$('solangStart')?.addEventListener('click', () => launch('project', 'solang', 'start'));
$('solangStop')?.addEventListener('click', () => launch('project', 'solang', 'stop'));
$('solangOpen')?.addEventListener('click', () => openUrl(SOLANG_URL));

$('solangForm')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = $('solangLinks');
  const btn = $('solangSubmitBtn');
  const status = $('solangStatusText');
  const resBox = $('solangResults');
  const links = (input.value || '').trim();
  if (!links) return;

  btn.disabled = true;
  status.textContent = 'Processing... Auto-starting Solang and analyzing tracks.';
  toast('Sending links to Solang Studio...');

  try {
    const r = await post('/api/action', { action: 'solang', op: 'bulk_add', links });
    if (r.ok) {
      toast(r.why || 'Ingested successfully!');
      status.textContent = r.why;
      input.value = '';
      if (Array.isArray(r.items) && r.items.length > 0) {
        setHTML(resBox, `<div class="meta small" style="margin-top:6px;"><b>Ingested:</b></div>` + r.items.map((it) => {
          const isDone = it.status === 'ingested' || it.status === 'already_exists';
          const icon = isDone ? '✓' : '✕';
          const cls = isDone ? 'ok' : 'bad';
          return `<div class="mjob" style="margin-top:4px;"><span class="jicon ${cls}">${icon}</span>`
            + `<span class="jt">${esc(it.title || it.videoId)}</span>`
            + `<span class="dim">${esc(it.artist || '')} · ${esc(it.status)}</span></div>`;
        }).join(''));
      }
    } else {
      toast(r.why || 'Solang ingest failed');
      status.textContent = r.why || 'Failed';
    }
  } catch (err) {
    toast('Error contacting HUD backend');
    status.textContent = 'Network error contacting HUD backend.';
  } finally {
    btn.disabled = false;
  }
});

// ---------- polling: every 10 s while visible, every 2 s for a minute after a start or stop
function refreshLaunchpad() { loadProjects(); loadMedia(); }
let launchpadTick = 0;
setInterval(() => {
  if (document.hidden) return;
  launchpadTick += 1;
  if (Date.now() < fastUntil || launchpadTick % 5 === 0) refreshLaunchpad();
}, 2000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshLaunchpad(); });
refreshLaunchpad();

// ---------- Ctrl+K palette entries (app.js asks for these when it builds the list)
function launchpadPaletteItems() {
  const items = [];
  for (const p of projCache.filter((x) => !x.hidden && !x.self)) {
    if (p.running && p.url) items.push({ label: `Open ${p.title}`, cat: 'Project', run: () => { closePalette(); openUrl(p.url); } });
    else if (p.can_start) items.push({ label: `Start ${p.title}`, cat: 'Project', run: () => { closePalette(); launch('project', p.name, 'start'); } });
    items.push({ label: `VS Code: ${p.title}`, cat: 'Project', run: () => { closePalette(); launch('project', p.name, 'editor'); } });
  }
  for (const t of mediaCache) {
    if (t.state === 'up' || t.state === 'busy') items.push({ label: `Open ${t.title}`, cat: 'Media', run: () => { closePalette(); openUrl(t.url); } });
    else if (t.can_start) items.push({ label: `Start ${t.title}`, cat: 'Media', run: () => { closePalette(); launch('media', t.name, 'start'); } });
  }
  return items;
}
