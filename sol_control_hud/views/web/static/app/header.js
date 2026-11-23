// model switcher and header/quickbar actions (split from app.js)
'use strict';
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

