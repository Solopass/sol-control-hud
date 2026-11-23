// overnight ask, chain controls, the reader (split from app.js)
'use strict';
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

