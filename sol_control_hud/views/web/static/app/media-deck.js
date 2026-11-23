// Media & Audio Deck (split from app.js)
'use strict';
// ---------- Media & Audio Deck (#c-media-ctl)
async function loadMediaSession() {
  if (document.hidden) return;
  if (typeof cardShown === 'function' && !cardShown('c-media-ctl')) return;
  try {
    const d = await (await fetch('/api/media-session', { cache: 'no-store' })).json();
    if (!d.ok) return;
    renderMediaDeck(d);
  } catch (e) { /* ignore */ }
}

function renderMediaDeck(d) {
  const titleEl = $('mediaDeckTitle');
  const artistEl = $('mediaDeckArtist');
  const statusEl = $('mediaDeckStatus');
  const appEl = $('mediaDeckApp');
  const playBtn = $('mediaBtnPlayPause');
  const artIcon = $('mediaArtIcon');

  if (titleEl) titleEl.textContent = d.title || (d.playing ? 'Active Media' : 'No active media');
  if (artistEl) artistEl.textContent = d.artist || (d.clean_app ? `via ${d.clean_app}` : '–');
  if (statusEl) statusEl.textContent = `${d.status || 'idle'}${d.clean_app ? ` · ${d.clean_app}` : ''}`;
  if (appEl) appEl.textContent = d.clean_app || 'GSMTC';

  if (playBtn) {
    playBtn.textContent = d.playing ? '⏸' : '▶';
    playBtn.title = d.playing ? 'Pause Playback' : 'Resume Playback';
  }
  if (artIcon) {
    artIcon.textContent = d.clean_app === 'Spotify' ? '🟢' : d.clean_app === 'Brave' || d.clean_app === 'Chrome' ? '🌐' : '🎵';
  }

  const sel = $('mediaTargetSelect');
  if (sel && d.menu && document.activeElement !== sel) {
    sel.innerHTML = d.menu.map((m) => {
      if (m.target == null) return '';
      const cleanLabel = (m.label || '').replace(/^[✓\s]+/, '').trim();
      return `<option value="${esc(m.target)}"${m.target === d.target ? ' selected' : ''}>${esc(cleanLabel)}</option>`;
    }).join('');
  }

  const strip = $('mediaSessionsStrip');
  if (strip && d.sessions) {
    strip.innerHTML = d.sessions.map((s) => {
      const isCur = s.title === d.title;
      return `<span class="media-sess-chip${isCur ? ' active' : ''}" title="${esc(s.app)}">${esc(s.app.split('.').pop() || s.app)}: ${esc((s.title || s.status).slice(0, 30))}</span>`;
    }).join('');
  }
}

function initMediaControls() {
  const prevBtn = $('mediaBtnPrev');
  const playBtn = $('mediaBtnPlayPause');
  const nextBtn = $('mediaBtnNext');
  const refreshBtn = $('mediaCtlRefresh');
  const targetSel = $('mediaTargetSelect');

  if (prevBtn) prevBtn.onclick = () => postMediaTransport('prev');
  if (playBtn) playBtn.onclick = () => postMediaTransport('play_pause');
  if (nextBtn) nextBtn.onclick = () => postMediaTransport('next');
  if (refreshBtn) refreshBtn.onclick = loadMediaSession;

  if (targetSel) {
    targetSel.onchange = async () => {
      const r = await post('/api/action', { action: 'media_target', target: targetSel.value });
      if (r?.target) toast(`Media keys routed to: ${r.target}`);
      loadMediaSession();
    };
  }
}

async function postMediaTransport(op) {
  const r = await post('/api/action', { action: 'media_transport', target: op });
  if (r?.ok) {
    setTimeout(loadMediaSession, 400);
  } else if (r?.why) {
    toast(r.why);
  }
}

// Global click handler delegation for service tiles
document.addEventListener('click', async (e) => {
  const svcBtn = e.target.closest('[data-svc-name]');
  if (svcBtn) {
    e.stopPropagation();
    const svc = svcBtn.dataset.svcName;
    const r = await post('/api/action', { action: 'svc_control', target: svc });
    if (r?.ok) toast(`Service action triggered: ${svc}`);
    else toast(r?.why || 'Failed to trigger service');
  }
});

