// iPad deck report and security (split from app.js)
'use strict';
// The iPad deck's own report (sol-deck's diag.js -> /api/clientlog): pairing, input
function renderSolPad(p) {
  if (typeof cardShown === 'function' && !cardShown('c-pad')) return;
  const sp = p.sol_pad || {};
  const pill = $('padPill');
  if (!pill) return;
  const states = {
    online: ['ONLINE', 'ok'],
    partial: ['PARTIAL', 'warn'],
    standby: ['USB READY', 'dim'],
    offline: ['OFFLINE', 'dim'],
  };
  const [label, cls] = states[sp.state] || ['…', 'dim'];
  pill.textContent = label;
  pill.className = `pill ${cls}`;
  const yn = (v, yes, no, warn) => (v == null ? '<span class="dim">–</span>' : v ? `<span style="color:var(--green)">${yes}</span>` : `<span style="color:${warn ? 'var(--amber)' : 'var(--dim)'}">${no}</span>`);
  setHTML($('padUsb'), yn(sp.usb_connected, 'Connected (USB)', 'Unplugged', false));
  setHTML($('padStream'), yn(sp.stream_running, 'Running', 'Stopped', false));
  setHTML($('padDock'), yn(sp.dock_running, 'Running', 'Stopped', false));
  const running = Boolean(sp.stream_running || sp.dock_running);
  if ($('padStartBtn')) $('padStartBtn').hidden = Boolean(sp.stream_running && sp.dock_running);
  if ($('padRestartBtn')) $('padRestartBtn').hidden = !running;
  if ($('padStopBtn')) $('padStopBtn').hidden = !running;

  const po = sp.popos || {};
  const poEl = $('padPoposStatus');
  if (poEl) {
    if (po.online) {
      const bits = [];
      if (po.mem_avail_gb != null) bits.push(`${po.mem_avail_gb}G free`);
      if (po.gpu_temp_c != null) bits.push(`GPU ${po.gpu_temp_c}°C`);
      if (po.docker_running != null) bits.push(`${po.docker_running} Docker`);
      const extra = bits.length ? ` <span class="dim">· ${esc(bits.join(' · '))}</span>` : '';
      poEl.innerHTML = `<span style="color:var(--green)">Awake</span>${extra}`;
    } else if (po.online === false) {
      poEl.innerHTML = `<span style="color:var(--amber)">Sleeping</span>`;
    } else {
      poEl.textContent = '–';
    }
  }
  if ($('padPoposWakeBtn')) $('padPoposWakeBtn').hidden = Boolean(po.online);
  if ($('padPoposSleepBtn')) $('padPoposSleepBtn').hidden = po.online === false;
}
// lag p50/p95, transport, errors. Empty when sol-deck sends none.
function deckHealthHtml(client) {
  const parts = [];
  parts.push(client.paired
    ? '<span style="color:var(--ok)">🔑 paired</span>'
    : '<span style="color:var(--warn)">not paired</span>');
  const h = client.health;
  if (h) {
    if (h.p50_ms != null && h.p95_ms != null) {
      const slow = h.p95_ms > 250;
      parts.push(`<span style="color:var(${slow ? '--warn' : '--ok'})">lag ${Math.round(h.p50_ms)}/${Math.round(h.p95_ms)} ms</span>`);
    }
    if (h.transport) parts.push(esc(h.transport));
    const errs = (h.errors || 0) + (h.stream_errors || 0);
    if (errs) parts.push(`<span style="color:var(--warn)">${errs} error${errs === 1 ? '' : 's'}</span>`);
    if (h.ws_drops) parts.push(`<span style="color:var(--warn)">${h.ws_drops} socket drop${h.ws_drops === 1 ? '' : 's'}</span>`);
    const age = h.age_sec < 90 ? `${h.age_sec}s` : h.age_sec < 5400 ? `${Math.round(h.age_sec / 60)}m` : `${Math.round(h.age_sec / 3600)}h`;
    parts.push(`report ${age} ago`);
  }
  return ` <span class="dim">· ${parts.join(' · ')}</span>`;
}

function renderSecurity(p) {
  if (typeof cardShown === 'function' && !cardShown('c-security')) return;
  const sec = p.security || {};
  const pill = $('secPill');
  const masterBtn = $('secMasterToggleBtn');
  const alertsList = $('secAlertsList');
  if (!pill || !masterBtn) return;

  const deckDot = $('secDeckDot');
  const deckTitle = $('secDeckTitle');
  const ipadStatus = $('secIpadStatus');
  const startBtn = $('secDeckStartBtn');
  const restartBtn = $('secDeckRestartBtn');
  const stopBtn = $('secDeckStopBtn');
  const proc = sec.process || {};
  const client = sec.client || {};

  if (!sec.up) {
    pill.className = 'pill dim';
    pill.textContent = 'OFFLINE';
    masterBtn.textContent = 'sol-deck Offline';
    masterBtn.disabled = true;
    masterBtn.className = 'mini ghost';
    if ($('secCanaryBadge')) { $('secCanaryBadge').textContent = 'OFF'; $('secCanaryBadge').className = 'badge dim'; }
    if ($('secArpBadge')) { $('secArpBadge').textContent = 'OFF'; $('secArpBadge').className = 'badge dim'; }
    if ($('secRadarBadge')) { $('secRadarBadge').textContent = '–'; $('secRadarBadge').className = 'badge dim'; }
    if ($('secSoundBadge')) { $('secSoundBadge').textContent = '–'; $('secSoundBadge').className = 'badge dim'; }
    if ($('secCanaryPorts')) $('secCanaryPorts').textContent = 'sol-deck (:5000) not running';
    if ($('secArpSub')) $('secArpSub').textContent = 'Start sol-deck service to enable';
    if (alertsList) alertsList.hidden = true;

    if (deckDot) deckDot.className = 'sec-link-dot off';
    if (deckTitle) {
      if (proc.running) {
        deckTitle.innerHTML = `SOL Deck Service: <strong style="color:var(--warn)">STARTING / PORT 5000 CLOSED</strong> (PID ${proc.pid})`;
      } else {
        deckTitle.innerHTML = `SOL Deck Service (:5000): <strong style="color:var(--bad)">OFFLINE (STOPPED)</strong>`;
      }
    }
    if (ipadStatus) ipadStatus.textContent = '📱 iPad Link: Offline (PC companion bridge is stopped)';
    if (startBtn) startBtn.hidden = false;
    if (restartBtn) restartBtn.hidden = !proc.running;
    if (stopBtn) stopBtn.hidden = !proc.running;
    const costEl = $('secProcCost');
    if (costEl) costEl.textContent = 'sol-deck offline · 0 MB RAM · 0% CPU';
    return;
  }

  // Update Link Bar when online
  if (deckDot) deckDot.className = 'sec-link-dot on';
  if (deckTitle) {
    const uptimeStr = proc.uptime_sec ? `${Math.floor(proc.uptime_sec / 60)}m` : '0m';
    const ramStr = proc.ram_mb ? `${proc.ram_mb} MB` : '~18 MB';
    deckTitle.innerHTML = `SOL Deck Service (:5000): <strong style="color:var(--ok)">ONLINE</strong> <span class="dim">(PID ${proc.pid || '–'} · ${ramStr} RAM · up ${uptimeStr})</span>`;
  }
  if (ipadStatus) {
    const health = deckHealthHtml(client);
    if (client.active) {
      ipadStatus.innerHTML = `📱 <strong style="color:var(--ok)">${esc(client.name || 'iPad mini')} (${esc(client.ip || '192.168.0.124')}):</strong> <span style="color:var(--ok)">${esc(client.status_text || 'Connected')}</span> <span class="dim">· ${client.request_count || 0} reqs</span>${health}`;
    } else if (client.ip) {
      ipadStatus.innerHTML = `📱 <span class="dim">${esc(client.name || 'iPad mini')} (${esc(client.ip)}):</span> <span style="color:var(--warn)">${esc(client.status_text || 'Standby')}</span>${health}`;
    } else {
      ipadStatus.innerHTML = `📱 <span class="dim">iPad mini: Waiting for client connection...</span>`;
    }
  }
  if (startBtn) startBtn.hidden = true;
  if (restartBtn) restartBtn.hidden = false;
  if (stopBtn) stopBtn.hidden = false;

  masterBtn.disabled = false;
  const s = sec.summary || {};
  const set = sec.settings || {};
  const alerts = sec.alerts || [];

  // Master status pill
  if (s.status === 'alert') {
    pill.className = 'pill bad';
    pill.textContent = '🚨 INTRUSION ACTIVE';
  } else if (s.status === 'warning') {
    pill.className = 'pill warn';
    pill.textContent = '⚠️ WARNING';
  } else if (!s.canary_active && !set.arp_watchdog_enabled) {
    pill.className = 'pill dim';
    pill.textContent = '⚪ DISABLED';
  } else {
    pill.className = 'pill ok';
    pill.textContent = '🛡️ SECURE';
  }

  // Master toggle button
  const anyActive = s.canary_active || set.arp_watchdog_enabled;
  if (anyActive) {
    masterBtn.textContent = 'Turn Off Watchdog';
    masterBtn.className = 'mini swap-run';
  } else {
    masterBtn.textContent = 'Turn On Watchdog';
    masterBtn.className = 'mini go';
  }

  // 1. Canary
  const cBadge = $('secCanaryBadge');
  const cBtn = $('secCanaryToggleBtn');
  if (cBadge && cBtn) {
    if (s.canary_active) {
      cBadge.textContent = 'ACTIVE';
      cBadge.className = 'badge ok';
      cBtn.textContent = 'Turn Off';
      cBtn.className = 'mini ghost';
    } else {
      cBadge.textContent = 'OFF';
      cBadge.className = 'badge dim';
      cBtn.textContent = 'Turn On';
      cBtn.className = 'mini go';
    }
  }
  const ports = (s.canary_ports && s.canary_ports.length) ? s.canary_ports.join(', ') : 'None';
  if ($('secCanaryPorts')) $('secCanaryPorts').textContent = `TCP Decoys: ${ports}`;

  // 2. ARP Guard
  const aBadge = $('secArpBadge');
  const aBtn = $('secArpToggleBtn');
  if (aBadge && aBtn) {
    if (!set.arp_watchdog_enabled) {
      aBadge.textContent = 'DISABLED';
      aBadge.className = 'badge dim';
      aBtn.textContent = 'Turn On';
      aBtn.className = 'mini go';
    } else if (s.gateway_status === 'verified') {
      aBadge.textContent = 'VERIFIED';
      aBadge.className = 'badge ok';
      aBtn.textContent = 'Turn Off';
      aBtn.className = 'mini ghost';
    } else {
      aBadge.textContent = 'SPOOF DETECTED';
      aBadge.className = 'badge bad';
      aBtn.textContent = 'Turn Off';
      aBtn.className = 'mini ghost';
    }
  }
  if ($('secArpSub')) $('secArpSub').textContent = `Gateway: ${set.expected_gateway_ip || '192.168.0.1'}`;

  // 3. Radar
  const rBadge = $('secRadarBadge');
  const rBtn = $('secRadarToggleBtn');
  const known = s.known_device_count || 0;
  const unknown = s.unknown_device_count || 0;
  if (rBadge && rBtn) {
    if (unknown > 0 && set.unknown_device_alerts) {
      rBadge.textContent = `${unknown} UNKNOWN`;
      rBadge.className = 'badge warn';
    } else {
      rBadge.textContent = `${known} KNOWN`;
      rBadge.className = 'badge ok';
    }
    rBtn.textContent = set.unknown_device_alerts ? 'Alerts ON' : 'Alerts OFF';
  }
  if ($('secRadarSub')) $('secRadarSub').textContent = `${unknown} Unknown Devices`;

  // 4. iPad Sound
  const sndBadge = $('secSoundBadge');
  const sndBtn = $('secSoundToggleBtn');
  if (sndBadge && sndBtn) {
    if (set.ipad_sound_alert) {
      sndBadge.textContent = 'SIREN ON';
      sndBadge.className = 'badge ok';
      sndBtn.textContent = 'Mute';
    } else {
      sndBadge.textContent = 'MUTED';
      sndBadge.className = 'badge dim';
      sndBtn.textContent = 'Unmute';
    }
  }

  // Alerts box
  const unacked = s.unacked_alerts || 0;
  if (alertsList) {
    if (unacked > 0 || alerts.length > 0) {
      const unackedAlerts = alerts.filter(a => !a.acked);
      if (unackedAlerts.length > 0) {
        alertsList.hidden = false;
        alertsList.innerHTML = `<div class="sec-alert-banner">` +
          `<strong>🚨 Intrusion Alert (${unackedAlerts.length}):</strong> ` +
          `<span>${esc(unackedAlerts[0].message)}</span> ` +
          `<span class="dim">(${esc(unackedAlerts[0].source_ip || '')} · ${esc(unackedAlerts[0].timestamp || '')})</span>` +
          `</div>`;
      } else {
        alertsList.hidden = true;
      }
    } else {
      alertsList.hidden = true;
    }
  }

  // Live overhead / resource stats
  const costEl = $('secProcCost');
  if (costEl) {
    if (proc.ram_mb) {
      costEl.textContent = `sol-deck PID ${proc.pid} · ${proc.ram_mb} MB RAM · <0.1% CPU · 2 Canary Sockets Idle`;
    } else {
      costEl.textContent = 'sol-deck ~18 MB RAM · <0.1% CPU · 2 Canary Sockets Idle';
    }
  }
}

