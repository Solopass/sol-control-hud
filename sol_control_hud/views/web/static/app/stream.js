// the /api/stream connection (split from app.js)
'use strict';
function connect() {
  if (es) return;
  es = new EventSource('/api/stream');
  let current = null;   // the first message has everything; the next ones only what changed (patch: parts, snap: fields)
  es.onmessage = (e) => {
    try {
      const d = JSON.parse(e.data);
      if (d.full) current = d.full;
      else if (current) { Object.assign(current, d.patch || {}); if (d.snap) current.snapshot = { ...current.snapshot, ...d.snap }; }
      else return;
      render(current);
    } catch (err) { console.error(err); }
  };
  es.onerror = () => { $('live').classList.remove('on'); };
}
function disconnect() { if (es) { es.close(); es = null; } }
document.addEventListener('visibilitychange', () => { if (document.hidden) disconnect(); else { loadHistory(); connect(); } });
setInterval(() => {
  if (document.hidden) return;
  const secs = Math.round((Date.now() - last) / 1000);
  $('age').innerHTML = !last ? 'connecting…' : secs > 8 ? `<span style="color:var(--red)">no update for ${secs}s</span>` : `live · ${new Date().toLocaleTimeString()}`;
}, 1000);
addEventListener('resize', drawCharts);

