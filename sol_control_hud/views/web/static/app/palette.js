// command palette (Ctrl+K) (split from app.js)
'use strict';
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

