// Overnight Runner (split from app.js)
'use strict';
// ---------- Overnight Runner
let overnightInit = false;
let overnightData = null;

function initOvernightEvents() {
  if (overnightInit) return;
  overnightInit = true;

  const btnStart = $('btnStartOvernight');
  if (btnStart) {
    btnStart.onclick = async () => {
      const checkedBoxes = Array.from(document.querySelectorAll('#overnightChainList input[type="checkbox"]:checked'));
      const chains = checkedBoxes.map((cb) => cb.value);
      if (!chains.length) {
        toast('Select at least one chain to run tonight');
        return;
      }
      const dispRadio = document.querySelector('input[name="overnightDisplayMode"]:checked');
      const displayMode = dispRadio ? dispRadio.value : 'dpms_off';
      const sleepDone = Boolean($('overnightSleepDone')?.checked);

      btnStart.disabled = true;
      const r = await post('/api/action', {
        action: 'start_overnight',
        chains,
        display_mode: displayMode,
        sleep_when_done: sleepDone,
      });
      toast(r?.why || (r?.ok ? 'Overnight run started' : 'Could not start overnight run'));
      btnStart.disabled = false;
      loadOvernight();
    };
  }

  const btnCancel = $('btnCancelOvernight');
  if (btnCancel) {
    btnCancel.onclick = async () => {
      if (!(await hudConfirm('Cancel the overnight run?'))) return;
      btnCancel.disabled = true;
      const r = await post('/api/action', { action: 'cancel_overnight' });
      toast(r?.why || 'Cancelled overnight run');
      btnCancel.disabled = false;
      loadOvernight();
    };
  }

  const presetsEl = $('overnightPresets');
  if (presetsEl) {
    presetsEl.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-preset]');
      if (!btn || !overnightData) return;
      const presetKey = btn.dataset.preset;
      const p = (overnightData.presets || {})[presetKey];
      if (!p) return;
      const targetChains = new Set(p.chains || []);
      document.querySelectorAll('#overnightChainList input[type="checkbox"]').forEach((cb) => {
        cb.checked = targetChains.has(cb.value);
      });
      toast(`Selected preset: ${p.label}`);
      const bedtimeCb = $('overnightBedtimeTrigger');
      if (bedtimeCb?.checked) {
        post('/api/action', {
          action: 'set_overnight_bedtime',
          enabled: true,
          chains: Array.from(targetChains),
        });
      }
    });
  }

  const bedtimeCb = $('overnightBedtimeTrigger');
  if (bedtimeCb) {
    bedtimeCb.addEventListener('change', async (e) => {
      const checkedBoxes = Array.from(document.querySelectorAll('#overnightChainList input[type="checkbox"]:checked'));
      const chains = checkedBoxes.map((cb) => cb.value);
      const dispRadio = document.querySelector('input[name="overnightDisplayMode"]:checked');
      const sleepDone = Boolean($('overnightSleepDone')?.checked);
      const res = await post('/api/action', {
        action: 'set_overnight_bedtime',
        enabled: e.target.checked,
        chains,
        display_mode: dispRadio ? dispRadio.value : 'dpms_off',
        sleep_when_done: sleepDone,
      });
      toast(res?.why || (e.target.checked ? 'Bedtime auto-start armed' : 'Bedtime auto-start disabled'));
      loadOvernight();
    });
  }

  const chainListEl = $('overnightChainList');
  if (chainListEl) {
    chainListEl.addEventListener('change', () => {
      const bCb = $('overnightBedtimeTrigger');
      if (bCb?.checked) {
        const checkedBoxes = Array.from(document.querySelectorAll('#overnightChainList input[type="checkbox"]:checked'));
        post('/api/action', {
          action: 'set_overnight_bedtime',
          enabled: true,
          chains: checkedBoxes.map((cb) => cb.value),
        });
      }
    });
  }
}

async function loadOvernight() {
  initOvernightEvents();
  if (typeof cardShown === 'function' && !cardShown('c-overnight')) return;
  let d;
  try {
    d = await (await fetch('/api/overnight', { cache: 'no-store' })).json();
  } catch (e) {
    return;
  }
  if (!d?.ok) return;
  overnightData = d;
  const state = d.state || {};
  const presets = d.presets || {};
  const chains = d.chains || [];

  const statusPill = $('overnightStatusPill');
  const banner = $('overnightActiveBanner');
  const btnStart = $('btnStartOvernight');
  const btnCancel = $('btnCancelOvernight');
  const bedtimeCb = $('overnightBedtimeTrigger');
  if (bedtimeCb && document.activeElement !== bedtimeCb) {
    bedtimeCb.checked = Boolean(state.bedtime_autostart);
  }

  if (state.active) {
    if (statusPill) {
      statusPill.textContent = 'running';
      statusPill.className = 'pill waiting live-pulse';
    }
    if (banner) banner.hidden = false;
    if (btnStart) btnStart.hidden = true;
    if (btnCancel) btnCancel.hidden = false;

    const currEl = $('overnightCurrentChain');
    if (currEl) currEl.innerHTML = `<b>Running:</b> ${esc(state.current || 'initializing…')}`;

    const elapsedEl = $('overnightElapsed');
    if (elapsedEl && state.started_at) {
      const min = Math.max(1, Math.round((Date.now() / 1000 - state.started_at) / 60));
      elapsedEl.textContent = `elapsed: ${min}m`;
    }

    const total = (state.completed?.length || 0) + (state.queue?.length || 0) + (state.current ? 1 : 0);
    const done = state.completed?.length || 0;
    const pct = total ? Math.round((done / total) * 100) : 0;
    const bar = $('overnightProgressBar');
    if (bar) {
      const inner = bar.querySelector('i');
      if (inner) inner.style.width = `${pct}%`;
    }

    const qInfo = $('overnightQueueInfo');
    if (qInfo) {
      const nextUp = (state.queue || []).join(' → ') || 'none (last chain)';
      qInfo.textContent = `Remaining queue: ${nextUp} · Completed: ${(state.completed || []).join(', ') || 'none yet'}`;
    }
  } else {
    if (statusPill) {
      if (state.bedtime_autostart) {
        statusPill.textContent = 'bedtime armed';
        statusPill.className = 'pill cyan';
      } else {
        statusPill.textContent = state.status || 'idle';
        statusPill.className = state.status === 'completed' ? 'pill ok' : 'pill';
      }
    }
    if (banner) banner.hidden = true;
    if (btnStart) btnStart.hidden = false;
    if (btnCancel) btnCancel.hidden = true;
  }

  // Render presets buttons if empty
  const presetsEl = $('overnightPresets');
  if (presetsEl && !presetsEl.children.length) {
    setHTML(presetsEl, Object.entries(presets).map(([k, p]) =>
      `<button class="mini ghost" data-preset="${esc(k)}" title="${esc(p.description || '')}">🌟 ${esc(p.label)}</button>`
    ).join(''));
  }

  // Render chains checklist if empty
  const chainListEl = $('overnightChainList');
  if (chainListEl && (chainListEl.querySelector('.empty') || !chainListEl.children.length)) {
    if (chains.length) {
      const selSet = new Set(state.selected_chains || []);
      setHTML(chainListEl, chains.map((c) =>
        `<label class="overnight-item">` +
        `<input type="checkbox" value="${esc(c.name)}"${selSet.has(c.name) ? ' checked' : ''}>` +
        `<span><b>${esc(c.name)}</b> <small class="dim">(${c.steps} steps · ${esc(c.model)})</small></span>` +
        `</label>`
      ).join(''));
    } else {
      setHTML(chainListEl, '<div class="empty">No chains found in 1Notebook/Chains</div>');
    }
  }
}

