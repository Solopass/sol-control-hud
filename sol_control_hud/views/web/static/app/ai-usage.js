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
  // time left from the window's end, not the tracker's stored minutes (the summary is written hourly)
  let win = (res.windows || {}).current;
  const left = win ? Math.floor((Date.parse(win.end) - Date.now()) / 60000) : -1;
  if (!(left > 0)) win = null;
  else win = { ...win, minutes_left: left };

  $('aiUsageWeek').textContent = res.week_id ? `· ${res.week_id}` : '';
  setHTML($('aiUsageStats'), [
    stat(fmtTokens(c.output_tokens || 0), `Today Claude output (${c.turns || 0} replies)`, 'var(--cyan)'),
    stat(fmtTokens(fresh), 'Today Claude fresh input', 'var(--text)'),
    stat(fmtTokens(c.cache_read_tokens || 0), 'Claude cache reads', c.cache_read_tokens ? 'var(--green)' : 'var(--dim)'),
    stat(win ? `${win.replies}` : '–', win ? `5-h window: ${fmtTokens(win.output_tokens)} out · ${Math.floor(win.minutes_left / 60)}h${String(win.minutes_left % 60).padStart(2, '0')} left` : '5-h window: none open', win ? 'var(--cyan)' : 'var(--dim)'),
    stat(fmtTokens(thisWeek), `Week Claude output${vsAvg == null ? '' : ` · ${vsAvg >= 0 ? '+' : ''}${vsAvg}% vs 4-wk avg`}`, vsAvg != null && vsAvg > 25 ? 'var(--amber, #f5a524)' : 'var(--text)'),
    stat(fmtTokens(ag.is_estimated ? agTok : (ag.output_tokens || 0)), ag.is_estimated ? `Today Antigravity (${ag.turns || 0} steps, est.)` : `Today Antigravity output (${ag.turns || 0} calls · ${fmtTokens(ag.input_tokens || 0)} fresh in)`, 'var(--cyan)'),
  ].join(''));

  // 12-week bars: Claude output per week (the newest is the week so far)
  const max = Math.max(1, ...weeks.map(outOf));
  setHTML($('aiUsageTrend'), weeks.length ? weeks.map((wk, i) => {
    const v = outOf(wk), h = Math.max(2, Math.round(v / max * 100));
    const cur = i === weeks.length - 1;
    return `<i style="height:${h}%" class="${cur ? 'cur' : ''}" title="Week of ${esc(wk.period)}: ${fmtTokens(v)} Claude output, ${((wk.claude || {}).turns) || 0} replies${cur ? ' (so far)' : ''}"></i>`;
  }).join('') : '');

  const ins = res.insights || {};
  // pace: a heads-up when this 5-hour window or this week runs past your own usual peak
  const pw = (ins.pace || {}).window, pk = (ins.pace || {}).week;
  const high = (pw && pw.level === 'high') || (pk && pk.level === 'high');
  const pill = $('aiUsagePill');
  if (pill) {
    pill.className = 'pill ' + (high ? 'warn' : 'ok');
    pill.textContent = high ? 'Pace: high' : 'Pace: normal';
    pill.title = [pw ? `This 5-h window: ${fmtTokens(pw.output_tokens)} out, ${Math.round(pw.share_of_p90 * 100)}% of your usual peak (${fmtTokens(pw.p90)})` : 'No 5-h window open',
      pk ? `This week: ${fmtTokens(pk.so_far)} so far, heading for ${fmtTokens(pk.projected)} (your biggest of the last 8 weeks: ${fmtTokens(pk.biggest_of_8)})` : ''].filter(Boolean).join('\n');
  }

  // a year of days, GitHub-style: columns are weeks (Monday on top), colour = output that day
  const days = ins.days || [];
  if (days.length) {
    const tot = (d) => (d.claude || 0) + (d.antigravity || 0);
    const nz = days.map(tot).filter((v) => v > 0).sort((a, b) => a - b);
    const q = (f) => nz.length ? nz[Math.min(nz.length - 1, Math.floor(f * nz.length))] : 1;
    const cuts = [q(0.25), q(0.5), q(0.75)];
    const lead = (new Date(days[0].day + 'T12:00:00').getDay() + 6) % 7;   // blanks so rows are weekdays
    const cells = Array(lead).fill('<i style="visibility:hidden"></i>');
    for (const d of days) {
      const v = tot(d);
      const lvl = !v ? '' : v <= cuts[0] ? 'l1' : v <= cuts[1] ? 'l2' : v <= cuts[2] ? 'l3' : 'l4';
      cells.push(`<i class="${lvl}" title="${esc(d.day)}: ${fmtTokens(d.claude)} Claude + ${fmtTokens(d.antigravity)} Antigravity output, ${d.calls} calls"></i>`);
    }
    setHTML($('aiUsageHeat'), cells.join(''));
  }

  const row = (a, b, title) => `<div class="row" title="${esc(title || a)}"><span>${esc(a)}</span><span class="dim">${esc(b)}</span></div>`;
  setHTML($('aiUsageSessions'), (ins.sessions || []).slice(0, 5).map((x) =>
    row(x.title, `${fmtTokens(x.output_tokens)} · ${x.calls}`, `${x.title} (${x.project}, ${x.provider}): ${x.calls} calls, ${x.subagent_calls} by subagents, ${fmtTokens(x.output_tokens)} output`)).join('') || '<div class="dim">nothing yet</div>');
  setHTML($('aiUsageProjects'), (ins.projects || []).slice(0, 5).map((x) =>
    row(x.project, x.commits == null ? `${fmtTokens(x.output_tokens)} · no git` : `${fmtTokens(x.output_tokens)} · ${x.commits} commits`,
        `${x.project}: ${x.calls} calls, ${fmtTokens(x.output_tokens)} output, ${x.commits == null ? 'no git repo found' : x.commits + ' commits'}${x.tokens_per_commit ? ', ' + fmtTokens(x.tokens_per_commit) + ' output per commit' : ''}`)).join('') || '<div class="dim">nothing yet</div>');

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

