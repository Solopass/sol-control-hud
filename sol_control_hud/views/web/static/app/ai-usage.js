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

  // Real usage is output + fresh input (input and cache writes); cache reads are cheap re-reads, shown apart.
  const fresh = (c.input_tokens || 0) + (c.cache_write_tokens || 0);
  const agTok = ag.estimated_tokens || 0;
  const weeks = (res.history && res.history.weeks) || [];
  const outOf = (wk) => ((wk && wk.claude) || {}).output_tokens || 0;
  const thisWeek = weeks.length ? outOf(weeks[weeks.length - 1]) : 0;
  const prev = weeks.slice(-5, -1).map(outOf).filter((v) => v > 0);
  const avg = prev.length ? prev.reduce((x, y) => x + y, 0) / prev.length : 0;
  // a fair comparison: this week so far against the same number of days of an average week
  const dayOfWeek = ((new Date().getDay() + 6) % 7) + 1;
  const vsAvg = avg ? Math.round((thisWeek / (avg * dayOfWeek / 7) - 1) * 100) : null;
  const win = (res.windows || {}).current;

  $('aiUsageWeek').textContent = res.week_id ? `· ${res.week_id}` : '';
  setHTML($('aiUsageStats'), [
    stat(fmtTokens(c.output_tokens || 0), `Today Claude output (${c.turns || 0} replies)`, 'var(--cyan)'),
    stat(fmtTokens(fresh), 'Today Claude fresh input', 'var(--text)'),
    stat(fmtTokens(c.cache_read_tokens || 0), 'Claude cache reads', c.cache_read_tokens ? 'var(--green)' : 'var(--dim)'),
    stat(win ? `${win.replies}` : '–', win ? `5-h window: ${fmtTokens(win.output_tokens)} out · ${Math.floor(win.minutes_left / 60)}h${String(win.minutes_left % 60).padStart(2, '0')} left` : '5-h window: none open', win ? 'var(--cyan)' : 'var(--dim)'),
    stat(fmtTokens(thisWeek), `Week Claude output${vsAvg == null ? '' : ` · ${vsAvg >= 0 ? '+' : ''}${vsAvg}% vs 4-wk avg`}`, vsAvg != null && vsAvg > 25 ? 'var(--amber, #f5a524)' : 'var(--text)'),
    stat(fmtTokens(agTok), `Today Antigravity (${ag.turns || 0} steps, est.)`, 'var(--cyan)'),
  ].join(''));

  // 12-week bars: Claude output per week (the newest is the week so far)
  const max = Math.max(1, ...weeks.map(outOf));
  setHTML($('aiUsageTrend'), weeks.length ? weeks.map((wk, i) => {
    const v = outOf(wk), h = Math.max(2, Math.round(v / max * 100));
    const cur = i === weeks.length - 1;
    return `<i style="height:${h}%" class="${cur ? 'cur' : ''}" title="Week of ${esc(wk.period)}: ${fmtTokens(v)} Claude output, ${((wk.claude || {}).turns) || 0} replies${cur ? ' (so far)' : ''}"></i>`;
  }).join('') : '');

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

