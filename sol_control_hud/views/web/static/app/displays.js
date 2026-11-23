// GPU with no monitor (split from app.js)
'use strict';
// ---------- is anything rendering on a GPU with no monitor?
// 10-08: nine apps were pinned to "power saving" = the Intel UHD 770, which drives no screen, so every frame was
// drawn on the weak chip and copied across to the Radeon. It felt like lag, and nothing on the machine said so.
// Two signals: the Windows setting (true even while an app is shut) and what is actually rendering there now.
async function loadDisplays() {
  if (typeof cardShown === 'function' && !cardShown('c-gpu')) return;   // hidden in this layout: not polled (layouts.js)
  let d;
  try { d = await (await fetch('/api/displays', { cache: 'no-store' })).json(); } catch (e) { return; }
  const box = $('gpuWrong');
  if (!box) return;
  if (!d.ok || !d.available) { box.hidden = true; return; }
  const busy = d.busy || [], pinned = d.power_saving_apps || [], dark = (d.dark || [])[0];
  let html = '';
  if (busy.length) {
    const who = busy.map((r) => `<b>${esc(r.name)}</b> ${r.percent}%`).join(', ');
    html = `⚠ ${who} rendering on ${esc(busy[0].adapter)}, which has no monitor — every frame is copied to the card that does.`
         + ` <span class="dim">Restart those apps, or set them to “Let Windows decide” in Settings › Display › Graphics.</span>`;
  } else if (d.setting_problem && dark) {
    html = `⚠ ${pinned.length} app(s) set to power saving (${esc(pinned.slice(0, 4).join(', '))}) while ${esc(dark)}`
         + ` has no monitor — they will render there and be copied across.`;
  }
  if (html) setHTML(box, html);
  box.hidden = !html;
}
setInterval(() => { if (!document.hidden) loadDisplays(); }, 10000);

