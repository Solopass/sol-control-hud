// AI Usage card (split from app.js)
'use strict';
// ---------- AI Usage (Claude + Antigravity)
function fmtTokens(n) {
  if (!n) return '0';
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}

async function loadAiUsage() {
  if (document.hidden) return;
  if (typeof cardShown === 'function' && !cardShown('c-ai-usage')) return;
  let res; try { res = await (await fetch('/api/ai-usage', { cache: 'no-store' })).json(); } catch (e) { return; }
  if (!res || !res.available) {
    setHTML($('aiUsageStats'), '<div class="dim">Usage tracker data not yet available</div>');
    return;
  }
  const stat = (v, label, color) => `<div><b style="color:${color || 'var(--text)'}">${v ?? '–'}</b><span>${label}</span></div>`;
  const t = res.today || {};
  const c = t.claude || {};
  const ag = t.antigravity || {};
  const w = res.weekly || {};

  const claudeTodayTok = (c.output_tokens || 0) + (c.input_tokens || 0);
  const claudeReadTok = c.cache_read_tokens || 0;
  const agTok = ag.estimated_tokens || 0;

  $('aiUsageWeek').textContent = res.week_id ? `· ${res.week_id}` : '';
  setHTML($('aiUsageStats'), [
    stat(fmtTokens(claudeTodayTok), `Today Claude (${c.turns || 0} turns)`, 'var(--cyan)'),
    stat(fmtTokens(claudeReadTok), 'Claude cache reads', claudeReadTok ? 'var(--green)' : 'var(--dim)'),
    stat(fmtTokens(agTok), `Today Antigravity (${ag.turns || 0} steps)`, 'var(--cyan)'),
    stat(fmtTokens(w.claude_tokens), `Week Claude (${w.claude_turns || 0} turns)`, 'var(--text)'),
    stat(fmtTokens(w.antigravity_tokens), `Week Antigravity (${w.antigravity_turns || 0} steps)`, 'var(--text)'),
  ].join(''));

  const models = (t.models || []).map(m => esc(m.model)).join(' · ');
  const projs = (t.projects || []).map(p => esc(p)).join(', ');
  const noteParts = [];
  if (models) noteParts.push(`Models: <b>${models}</b>`);
  if (projs) noteParts.push(`Projects: ${projs}`);
  if (res.last_updated) noteParts.push(`Updated: ${res.last_updated}`);
  setHTML($('aiUsageSub'), noteParts.join(' · ') || 'No activity recorded today.');
}
setInterval(loadAiUsage, 60000);
document.addEventListener('visibilitychange', () => { if (!document.hidden && !$('aiUsageStats')?.children.length) loadAiUsage(); });

