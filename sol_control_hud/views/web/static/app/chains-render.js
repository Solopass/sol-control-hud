// chain list rendering (split from app.js)
'use strict';
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

