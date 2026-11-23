// Quick AI Console (split from app.js)
'use strict';
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

