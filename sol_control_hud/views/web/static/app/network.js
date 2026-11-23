// Network / Pi-hole card (split from app.js)
'use strict';
// Network · Pi-hole (collectors/network.py: probed every 30 s in its own thread, no password needed)
const NET_STATE = {
  ok: ['OK', 'ok'], slow: ['SLOW', 'warn'], degraded: ['ONE IP DOWN', 'warn'], 'dns-off': ['DNS OFF', 'bad'],
  'no-upstream': ['NO UPSTREAM', 'bad'], 'not-blocking': ['NOT BLOCKING', 'warn'], down: ['DOWN', 'bad'], unknown: ['…', 'dim'],
};
const TS_SVG_UP = '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><circle cx="3" cy="3" r="1.5"/><circle cx="8" cy="3" r="1.5" opacity=".35"/><circle cx="13" cy="3" r="1.5"/><circle cx="3" cy="8" r="1.5"/><circle cx="8" cy="8" r="1.5"/><circle cx="13" cy="8" r="1.5"/><circle cx="3" cy="13" r="1.5" opacity=".35"/><circle cx="8" cy="13" r="1.5"/><circle cx="13" cy="13" r="1.5" opacity=".35"/></svg>';
const TS_SVG_WARN = '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><circle cx="3" cy="3" r="1.5" opacity=".35"/><circle cx="8" cy="3" r="1.5" opacity=".35"/><circle cx="13" cy="3" r="1.5" opacity=".35"/><circle cx="3" cy="8" r="1.5"/><circle cx="8" cy="8" r="1.5"/><circle cx="13" cy="8" r="1.5"/><circle cx="3" cy="13" r="1.5" opacity=".35"/><circle cx="8" cy="13" r="1.5" opacity=".35"/><circle cx="13" cy="13" r="1.5" opacity=".35"/><path d="M2 14L14 2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';

function renderHeaderTailscale(ts) {
  const el = $('hdrTs');
  if (!el) return;
  if (!ts.running && !ts.up) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  if (ts.up) {
    el.className = 'hdr-ts up';
    let rem = '';
    if (ts.remaining_s != null) {
      const mins = Math.ceil(ts.remaining_s / 60);
      const h = Math.floor(mins / 60), m = mins % 60;
      rem = ` · closes in ${h > 0 ? `${h}h ${m}m` : `${m}m`}`;
    }
    el.title = `Tailscale UP (remote connections allowed)${rem} — click to jump to Network`;
    setHTML(el, `${TS_SVG_UP}<span>TS</span>`);
  } else {
    el.className = 'hdr-ts warn';
    el.title = 'Tailscale is ON, but tunnel is DOWN (cannot remote in) — click to jump to Network';
    setHTML(el, `${TS_SVG_WARN}<span>TS!</span>`);
  }
}

function renderNetwork(p) {
  const net = p.network || {}, ph = net.pihole || {}, lan = net.lan || {};
  renderHeaderTailscale(net.tailscale || {});
  if (typeof cardShown === 'function' && !cardShown('c-net')) return;
  const [label, cls] = NET_STATE[ph.state] || NET_STATE.unknown;
  const pill = $('netPill'); if (!pill) return;
  pill.textContent = label; pill.className = `pill ${cls}`; pill.title = ph.alert || '';
  const yn = (v, yes, no) => (v == null ? '<span class="dim">–</span>' : v ? `<span style="color:var(--green)">${yes}</span>` : `<span style="color:var(--red)">${no}</span>`);
  setHTML($('netHosts'), (ph.hosts || []).map((h) => {
    const st = h.up == null ? '<span class="dim">checking…</span>'
      : !h.up ? '<span style="color:var(--red)">no answer</span>'
      : !h.dns ? '<span style="color:var(--red)">DNS service off</span>'
      : `<span style="color:${h.ms > 150 ? 'var(--amber)' : 'var(--green)'}">up · ${h.ms} ms</span>`;
    return `<div class="row"><span>${esc(h.ip)}</span><span>${st}</span></div>`;
  }).join('') || '<div class="row dim">no Pi-hole configured</div>');
  const pc = ph.pc || {};
  setHTML($('netPc'), `${yn(pc.blocking, 'blocking', 'not blocking')} · ${yn(pc.upstream, 'upstream ok', 'upstream fails')}`);
  const note = $('netNote'); note.hidden = !ph.note; note.textContent = ph.note || '';
  // Stats and pause/resume come through sol-deck, which holds the Pi-hole app password.
  const st = ph.stats || {}, statsEl = $('netStats'), acts = $('netStatsActs');
  if (statsEl && acts) {
    if (st.state === 'ok') {
      const n = (v) => (v == null ? '–' : Number(v).toLocaleString());
      const paused = st.blocking === false;
      setHTML(statsEl, `${n(st.queries)} queries · ${st.percent ?? '–'}% blocked`
        + (paused ? ` · <span style="color:var(--amber)">paused${st.timer ? `, back in ${Math.ceil(st.timer / 60)} min` : ''}</span>` : ''));
      $('netPauseBtn').hidden = paused;
      $('netResumeBtn').hidden = !paused;
      $('netTop').textContent = (st.top_blocked || []).length ? `top blocked: ${st.top_blocked.slice(0, 3).join(', ')}` : '';
      acts.hidden = false;
    } else {
      statsEl.innerHTML = `<span class="dim">${esc(st.why || '–')}</span>`;
      acts.hidden = true;
    }
  }
  const ts = net.tailscale || {}, tsRow = $('netTailscaleRow');
  if (tsRow) {
    tsRow.hidden = ts.installed === false;
    const tsState = $('netTailscaleState');
    if (tsState) {
      if (ts.up) setHTML(tsState, `<span style="color:var(--green)">up${ts.tray ? ' · tray' : ''}</span>`);
      else if (ts.running) setHTML(tsState, '<span style="color:var(--amber)">running (no tunnel)</span>');
      else setHTML(tsState, '<span class="dim">stopped</span>');
    }
    const tsTimer = $('netTailscaleTimer');
    if (tsTimer) {
      if (ts.up && ts.remaining_s != null) {
        const mins = Math.ceil(ts.remaining_s / 60);
        const h = Math.floor(mins / 60), m = mins % 60;
        tsTimer.textContent = `· closes in ${h > 0 ? `${h}h ${m}m` : `${m}m`}`;
        tsTimer.hidden = false;
      } else {
        tsTimer.hidden = true;
      }
    }
    if ($('netTsOpenBtn')) $('netTsOpenBtn').hidden = Boolean(ts.running && ts.tray);
    const upBtn = $('netTsUpBtn');
    if (upBtn) {
      upBtn.hidden = !ts.running;
      upBtn.textContent = ts.up ? '↻ +12h' : '▲ Up (12h)';
      upBtn.title = ts.up ? 'Reset the auto-close timer to 12 hours' : 'Run tailscale up to allow remote connections (auto-closes after 12h)';
    }
    if ($('netTsCloseBtn')) $('netTsCloseBtn').hidden = !ts.running && !ts.tray;
  }
  const devs = lan.devices || [];
  $('netCount').textContent = devs.length ? `${lan.online || 0} online · ${devs.length} known${lan.new ? ` · ${lan.new} new` : ''}` : '';
  setHTML($('netDevs'), devs.map((d) => {
    const c = d.new ? 'new' : d.online ? '' : 'off';
    const seen = d.online ? 'online' : `seen ${esc(when(d.last_seen))}`;
    const tip = `${d.mac} · first seen ${d.first_seen}${d.private ? ' · private (randomised) MAC: may change' : ''}`;
    return `<span class="ip ${c}">${esc(d.ip)}</span><span class="${c}" title="${esc(tip)}">${d.new ? 'NEW · ' : ''}${esc(d.name || (d.private ? 'phone/tablet (private MAC)' : 'unnamed'))}</span><span class="dim ${c}">${seen}</span>`;
  }).join('') || '<span class="dim">scanning…</span>');
}

