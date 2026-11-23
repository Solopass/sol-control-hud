// Obsidian notes widget (split from app.js)
'use strict';
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

