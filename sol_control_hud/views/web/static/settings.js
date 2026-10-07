// ⚙ Settings: everything you can change about the app, the ticker and the local AI in one place.
// Reads GET /api/settings; each change is one POST (action "setting"), checked by settings_api.py on the server.
// Loaded after app.js and uses its helpers ($, esc, post, toast, setHTML).
'use strict';

function swRow(key, label, on, hint = '') {
  return `<label class="srow"><span><b>${esc(label)}</b>${hint ? `<span class="dim small">${esc(hint)}</span>` : ''}</span>`
    + `<input type="checkbox" class="switch" data-setting="${esc(key)}"${on ? ' checked' : ''}></label>`;
}

function infoRow(label, hint) {
  return `<div class="srow"><span><b>${esc(label)}</b><span class="dim small">${esc(hint)}</span></span></div>`;
}

async function loadSettings() {
  let d;
  try { d = await (await fetch('/api/settings', { cache: 'no-store' })).json(); } catch (e) { return; }
  const a = d.app || {}, t = d.ticker || {};
  let html = '<section><h3>App</h3>'
    + swRow('ticker_shown', 'Show the taskbar ticker', a.ticker_shown, 'when hidden it stays in the tray')
    + swRow('start_at_login', 'Start when Windows starts', a.start_at_login)
    + swRow('dashboard_at_start', 'Open this dashboard when it starts', a.dashboard_at_start)
    + swRow('notify', 'Notifications', a.notify, 'Away results, chain failures, crashes, disks filling, VRAM spills')
    + `<div class="srow"><span><b>Theme</b><span class="dim small">the ticker uses the same one</span></span><select id="setTheme">${$('theme').innerHTML}</select></div>`
    + '</section>';
  if (t.available) {
    const preset = t.presets.find((p) => p.key === t.preset);
    html += '<section><h3>Ticker</h3>'
      + `<div class="srow"><span><b>Performance</b><span class="dim small">${esc(preset ? preset.desc : 'your own mix, set in the ticker dialog')}</span></span>`
      + `<select data-setting="preset">${t.presets.map((p) => `<option value="${p.key}"${p.key === t.preset ? ' selected' : ''}>${esc(p.name)}</option>`).join('')}`
      + `${t.preset === 'custom' ? '<option value="" selected disabled>Custom</option>' : ''}</select></div>`
      + `<div class="srow"><span><b>Seconds per slide</b></span><input type="number" min="2" max="60" value="${t.interval_seconds}" data-setting="interval_seconds" class="snum"></div>`
      + `<div class="srow"><span><b>Text size</b></span><select data-setting="font_scale">${t.font_scales.map((f) => `<option value="${f.key}"${f.key === t.font_scale ? ' selected' : ''}>${esc(f.name)}</option>`).join('')}</select></div>`
      + swRow('auto_hide_fullscreen', 'Hide during fullscreen games and videos', t.auto_hide_fullscreen)
      + swRow('alerts_pulse', 'Pulse on alerts', t.alerts_pulse)
      + swRow('alerts_sound', 'Sound on alerts', t.alerts_sound)
      + `<div class="srow col"><span><b>Slides</b><span class="dim small">what the ticker rotates through</span></span><div class="schips">`
      + t.slides.map((s) => `<label><input type="checkbox" data-setting="slide:${s.tag}"${s.on ? ' checked' : ''}>${esc(s.label)}</label>`).join('')
      + '</div></div></section>';
  }
  html += '<section><h3>Local AI</h3>'
    + swRow('hud_heal', 'Let the HUD reload a spilled model too', a.hud_heal,
      'off by default: the AI watcher already does it, and waits while a chain or chat uses the model')
    + infoRow('AI on / off, load and unload models', 'on the Local AI card')
    + infoRow('Chain schedules', 'the ⏱ button on each chain in the Chains card')
    + '</section>';
  setHTML($('settingsBody'), html);
  const th = $('setTheme');
  if (th) {
    th.value = $('theme').value;
    th.onchange = () => { $('theme').value = th.value; $('theme').dispatchEvent(new Event('change')); };
  }
}

async function changeSetting(el) {
  const key = el.dataset.setting, value = el.type === 'checkbox' ? el.checked : el.value;
  const r = await post('/api/action', { action: 'setting', target: key, value });
  toast(r.why || (r.ok ? 'saved' : 'that did not work'));
  setTimeout(loadSettings, 700);           // the ticker applies it on its own thread
}

$('settingsBody').addEventListener('change', (e) => {
  const el = e.target.closest('[data-setting]');
  if (el) changeSetting(el);
});
$('settingsBtn').onclick = () => { $('settingsModal').hidden = false; loadSettings(); };
$('settingsClose').onclick = () => { $('settingsModal').hidden = true; };
$('settingsModal').addEventListener('click', (e) => { if (e.target.id === 'settingsModal') $('settingsModal').hidden = true; });
addEventListener('keydown', (e) => { if (e.key === 'Escape') $('settingsModal').hidden = true; });
