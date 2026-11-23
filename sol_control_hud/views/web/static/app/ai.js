// AI engine, answer speed, models (split from app.js)
'use strict';
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

