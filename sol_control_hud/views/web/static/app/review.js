// exam review (split from app.js)
'use strict';
// ---------- the stream (only while this tab is visible)
// the Exam assist card (exam_review.py): solves practice exam questions in real time
const REV_PILL = { watching: 'ok', reading: '', answering: '', waiting: 'warn', paused: 'warn', error: 'bad', stopped: '' };
let lastReviewState = null;

function formatVerificationText(c) {
  if (!c) return '';
  const parts = [];
  if (c.question) parts.push(`Question:\n${c.question}`);
  if (c.exhibit_text) parts.push(`Exhibit / Context:\n${c.exhibit_text}`);
  if (c.question_type === 'matching' && c.matching_pairs) {
    parts.push('Matching Pairs:\n' + c.matching_pairs.map(p => `- ${p.source} ➔ ${p.target}`).join('\n'));
  } else if (c.question_type === 'fill_in_the_blank' && c.blank_answers) {
    parts.push('Command / Value:\n' + c.blank_answers.join('\n'));
  } else if (c.question_type === 'ordering' && c.ordered_sequence) {
    parts.push('Ordered Sequence:\n' + c.ordered_sequence.join('\n'));
  } else if (c.choices && c.choices.length) {
    parts.push('Choices:\n' + c.choices.map(o => `${o.label}. ${o.text}`).join('\n'));
  }
  const ans = c.answer || (c.answer_labels || []).join(', ');
  if (ans) parts.push(`Suggested Answer:\n${ans}`);
  if (c.topic) parts.push(`Topic: ${c.topic}`);
  if (c.explanation) parts.push(`Explanation:\n${c.explanation}`);
  return parts.join('\n\n');
}

function buildBraveAiQuery(c) {
  if (!c) return '';
  const q = (c.question || '').trim();
  let combined = q;
  if (c.choices && c.choices.length) {
    combined = `${q} ${c.choices.map(o => `${o.label}) ${o.text}`).join(' ')}`.trim();
  } else if (c.answer) {
    combined = `${q} Answer: ${c.answer}`.trim();
  }
  if (combined.length > 350) {
    combined = combined.substring(0, 350).replace(/\s+\S*$/, '');
  }
  return `${combined} ??`;
}

function renderReview(p) {
  const r = p.review; if (!r) return;
  lastReviewState = r;
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
  const braveTop = $('revBraveTopBtn'); if (braveTop) braveTop.disabled = !c;
  const snipBtn = $('revSnipBtn'); if (snipBtn) { snipBtn.textContent = r.parts_count > 1 ? `➕ Add Snip (${r.parts_count} parts)` : '➕ Add Snip (Scroll)'; }
  const hudCb = $('revToggleHud'); if (hudCb && document.activeElement !== hudCb) { hudCb.checked = !!r.hud_enabled; }
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
      const ansLabels = c.answer_labels || c.correct_labels || [];
      const mark = (lb) => ansLabels.includes(lb) ? 'right' : (c.your_labels || []).includes(lb) ? 'mine' : '';
      contentHtml = (c.choices || []).map((o) => {
        const isRight = ansLabels.includes(o.label);
        const box = isRight ? '☑ ' : '☐ ';
        return `<div class="rev-opt ${mark(o.label)}"><b>${box}${esc(o.label)}.</b> ${esc(o.text)}` +
          `${(c.your_labels || []).includes(o.label) ? ' <span class="dim small">your selection</span>' : ''}` +
          `${isRight ? ` <span class="small" style="color:${isGemini ? 'var(--cyan)' : 'var(--green)'}">✓ answer</span>` : ''}</div>`;
      }).join('');
    }

    const exhibitHtml = c.exhibit_text ? `<details style="margin:6px 0;font-size:0.85rem"><summary class="dim" style="cursor:pointer">📊 Exhibit / Diagram</summary><pre style="background:var(--card);padding:6px;border-radius:4px;overflow-x:auto">${esc(c.exhibit_text)}</pre></details>` : '';
    const typeLabel = qType === 'matching' ? 'Matching' : qType === 'fill_in_the_blank' ? 'Fill-in-Blank' : qType === 'ordering' ? 'Ordering' : '';
    const typeBadge = typeLabel ? ` <span class="pill" style="font-size:0.75rem;padding:2px 6px">${typeLabel}</span>` : '';
    const modelBadge = isGemini ? ' <span class="pill" style="background:#0284c7;color:#fff;font-size:0.75rem;padding:2px 6px">✨ Gemini 3.8 Flash</span>' : '';
    let ansTitle = c.answer || (c.answer_labels || []).join(', ');
    if (qType === 'matching' && c.matching_pairs && c.matching_pairs.length) {
      ansTitle = `${c.matching_pairs.length} Pairs Matched`;
    } else if (qType === 'ordering' && c.ordered_sequence && c.ordered_sequence.length) {
      ansTitle = `${c.ordered_sequence.length} Steps Sequenced`;
    } else if (qType === 'fill_in_the_blank' && c.blank_answers && c.blank_answers.length) {
      ansTitle = c.blank_answers[0];
    }
    const head = `<span style="color:${isGemini ? 'var(--cyan)' : 'var(--green)'}">💡 Answer: ${esc(ansTitle)} · ${esc(c.topic || 'Solved')}</span>${typeBadge}${modelBadge}`;
    const whyWrong = c.why_previous_wrong ? `<p class="rev-explain" style="color:var(--amber);margin-bottom:6px"><b>Previous attempt issue:</b> ${esc(c.why_previous_wrong)}</p>` : '';
    const body = `${whyWrong}<p class="rev-explain">${esc(c.explanation || '')}</p>`;
    const autoScrollBtn = `<button class="mini primary" style="margin-top:8px" data-act="review" data-target="auto_scroll" title="Simulate scroll down, stitch, and solve">⚡ Auto-Scroll & Solve</button>`;
    const retryBtn = isGemini ? '' : `<button class="mini danger" style="margin-top:8px" data-act="review" data-target="retry_gemini" title="Verify or re-solve with Gemini 3.8 Flash">🔍 Verify / Retry (Gemini)</button>`;
    const braveBtn = `<button class="mini" style="margin-top:8px;background:#ea580c;color:#fff" data-act="review" data-target="brave_search" title="Copy question & choices to clipboard and search in Brave using AI mode">🦁 Brave AI Search</button>`;
    setHTML(cur, `<div class="row"><b>${head}</b><span class="dim small">${esc(c.at || '')}${c.seconds ? ` · ${c.seconds} s` : ''}</span></div>` +
      `<div class="rev-q">${esc(c.question)}</div>${exhibitHtml}${contentHtml}${body}<div style="display:flex;gap:8px;flex-wrap:wrap">${autoScrollBtn}${retryBtn}${braveBtn}</div>`);
  }
  setHTML($('revHistory'), (r.history || []).map((h) => `<div class="item"><span>${h.n}. ${esc(h.topic || h.question)}</span>` +
    `<span style="color:${h.gemini ? 'var(--cyan)' : 'var(--green)'}">${h.gemini ? '✨' : '✓'}</span></div>`).join('') || '<div class="empty">nothing yet</div>');
  const topics = Object.entries(r.topics || {}).sort((a, b) => b[1] - a[1]);
  setHTML($('revTopics'), topics.map(([t, n]) => `<div class="item"><span>${esc(t)}</span><span style="color:var(--cyan)">${n}×</span></div>`).join('')
    || '<div class="empty">no topics recorded yet</div>');
}

