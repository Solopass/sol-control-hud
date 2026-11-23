// start-up: first load and timers (split from app.js)
'use strict';
(async function start() {
  try {
    const meta = await (await fetch('/api/meta', { cache: 'no-store' })).json();
    themes = meta.themes || {};
    $('theme').innerHTML = Object.entries(themes).map(([k, t]) => `<option value="${k}">${esc(t.name)}</option>`).join('');
    applyTheme(meta.theme);
  } catch (e) { /* default colors */ }
  await loadHistory();
  loadControl();
  loadWeek();
  loadPorts();
  loadDisplays();
  loadGuard();
  await initVaults();
  loadNotes();
  loadOvernight();
  initPlayground();
  initProcs();
  initMediaControls();
  initQuickSearch();
  loadProcs();
  loadMediaSession();
  setInterval(() => { if (!document.hidden) loadOvernight(); }, 4000);
  setInterval(() => { if (!document.hidden) { loadProcs(); loadMediaSession(); } }, 4000);
  if (!document.hidden) connect();
})();
