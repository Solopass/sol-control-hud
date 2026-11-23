// charts (canvas, last hour) (split from app.js)
'use strict';
// ---------- charts (canvas, last hour)
const SERIES_COLOR = { gpu: '--cyan', temp: '--amber', vram: '--cyan', ram: '--green', cpu: '--cyan', down: '--cyan', up: '--amber' };
function css(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }

const chartVisibility = new WeakMap();
const chartObserver = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    chartVisibility.set(entry.target, entry.isIntersecting);
  }
}, { rootMargin: '100px 0px' });

function drawChart(cv) {
  if (!chartVisibility.has(cv)) {
    chartObserver.observe(cv);
    chartVisibility.set(cv, true);
    initChartCrosshair(cv);
  }
  if (!chartVisibility.get(cv)) return; // off-screen optimization

  const names = cv.dataset.series.split(','), maxes = cv.dataset.max.split(',');
  const dpr = devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
  if (!w || !h) return;
  if (cv.width !== Math.round(w * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
  const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
  g.strokeStyle = css('--line'); g.lineWidth = 1; g.setLineDash([2, 4]);
  for (const f of [0.25, 0.5, 0.75]) { g.beginPath(); g.moveTo(0, h * f); g.lineTo(w, h * f); g.stroke(); }
  g.setLineDash([]);
  const span = SPAN[range], now = Date.now() / 1000, t0 = now - span, ts = hist.t;
  if (ts.length < 2) { g.fillStyle = css('--dim'); g.font = '11px Segoe UI'; g.fillText('collecting the last hour…', 6, h / 2); return; }
  names.forEach((name, si) => {
    const ys = hist[name]; let max = parseFloat(maxes[si] ?? maxes[0]);
    if (name === 'vram' && payload?.snapshot?.vram_total_gb) max = payload.snapshot.vram_total_gb;
    if (name === 'ram' && payload?.snapshot?.ram_total_gb) max = payload.snapshot.ram_total_gb;
    if (Number.isNaN(max)) max = Math.max(64, ...names.flatMap((n) => hist[n].filter((v) => v != null))) * 1.15;
    const color = css(SERIES_COLOR[name] || '--cyan');
    g.beginPath(); let started = false, lastX = 0;
    for (let i = 0; i < ts.length; i++) {
      if (ys[i] == null || ts[i] < t0) continue;
      const x = ((ts[i] - t0) / span) * w, y = h - 2 - Math.min(1, ys[i] / max) * (h - 6);
      if (!started) { g.moveTo(x, y); started = true; } else g.lineTo(x, y);
      lastX = x;
    }
    if (!started) return;
    g.strokeStyle = color; g.lineWidth = 1.6; g.lineJoin = 'round'; g.stroke();
    if (si === 0) {   // the first series gets a soft area fill
      g.lineTo(lastX, h); g.lineTo(((ts.find((t) => t >= t0) - t0) / span) * w, h); g.closePath();
      const grad = g.createLinearGradient(0, 0, 0, h); grad.addColorStop(0, color + '55'); grad.addColorStop(1, color + '00');
      g.fillStyle = grad; g.fill();
    }
  });

  // Render crosshair if cursor is hovering
  if (cv._hoverX != null && cv._hoverX >= 0 && cv._hoverX <= w) {
    g.strokeStyle = css('--cyan');
    g.lineWidth = 1;
    g.setLineDash([2, 2]);
    g.beginPath();
    g.moveTo(cv._hoverX, 0);
    g.lineTo(cv._hoverX, h);
    g.stroke();
    g.setLineDash([]);
  }

  g.fillStyle = css('--dim'); g.font = '10px Segoe UI'; g.fillText({ '1h': '60 min', '24h': '24 h', '7d': '7 days' }[range], 4, h - 4); g.fillText('now', w - 22, h - 4);
}

function initChartCrosshair(cv) {
  let tip = null;
  cv.addEventListener('mousemove', (e) => {
    const rect = cv.getBoundingClientRect();
    const x = e.clientX - rect.left;
    cv._hoverX = x;
    const w = cv.clientWidth, span = SPAN[range], now = Date.now() / 1000, t0 = now - span, ts = hist.t;
    if (ts.length < 2 || w <= 0) return;
    const targetT = t0 + (x / w) * span;
    let closestIdx = 0, minDiff = Infinity;
    for (let i = 0; i < ts.length; i++) {
      const diff = Math.abs(ts[i] - targetT);
      if (diff < minDiff) { minDiff = diff; closestIdx = i; }
    }
    const names = cv.dataset.series.split(',');
    const lines = names.map((n) => {
      const val = hist[n]?.[closestIdx];
      const unit = n.includes('kb') || n === 'down' || n === 'up' ? ' KB/s' : n.includes('temp') ? '°C' : (n === 'ram' || n === 'vram') ? ' GB' : '%';
      return `${n}: <b>${val != null ? (typeof val === 'number' ? val.toFixed(1) : val) : '–'}${unit}</b>`;
    });
    const timeStr = new Date(ts[closestIdx] * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    if (!tip) {
      tip = document.createElement('div');
      tip.className = 'chart-tip';
      cv.parentElement.appendChild(tip);
    }
    tip.style.left = `${x}px`;
    tip.style.top = `${e.clientY - rect.top}px`;
    tip.innerHTML = `<span class="dim">${timeStr}</span> · ${lines.join(' · ')}`;
    tip.hidden = false;
    drawChart(cv);
  });
  cv.addEventListener('mouseleave', () => {
    cv._hoverX = null;
    if (tip) tip.hidden = true;
    drawChart(cv);
  });
}
let drawQueued = false;
function drawCharts() {
  if (drawQueued) return; drawQueued = true;
  requestAnimationFrame(() => { drawQueued = false; document.querySelectorAll('canvas.chart').forEach(drawChart); });
}
function addPoint(p) {
  if (range !== '1h') return;          // the 24 h / 7 d charts come from the minute history (reloaded every minute)
  if (!p || (hist.t.length && p.t <= hist.t[hist.t.length - 1])) return;
  for (const k of Object.keys(hist)) hist[k].push(p[k] ?? null);
  const cut = p.t - 3600;
  while (hist.t.length && hist.t[0] < cut) for (const k of Object.keys(hist)) hist[k].shift();
}
async function loadHistory() {
  try { const h = await (await fetch(`/api/history?range=${range}`, { cache: 'no-store' })).json(); for (const k of Object.keys(hist)) hist[k] = h[k] || []; drawCharts(); } catch (e) { /* the stream fills it */ }
}

