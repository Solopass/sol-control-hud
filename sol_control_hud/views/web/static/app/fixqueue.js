// fix queue (split from app.js)
'use strict';
async function loadFixQueue() {
  const el = $('fixQueueList');
  if (!el) return;
  try {
    const data = await (await fetch('/api/fix-queue', { cache: 'no-store' })).json();
    const pill = $('fixQueuePill');
    if (pill) {
      pill.textContent = `${data.total_open || 0} open`;
      pill.className = `pill ${data.total_open > 0 ? 'active' : 'idle'}`;
    }
    const items = [];
    (data.green || []).forEach((g) => {
      items.push(`<div class="srow"><span><b style="color:var(--green)">✓ GREEN</b> <b>${esc(g.repo)}</b>: ${esc(g.task)} <code class="small">ai/${esc(g.branch || '')}</code></span></div>`);
    });
    (data.open || []).forEach((o) => {
      items.push(`<div class="srow"><span><b>${esc(o.repo)}</b>: ${esc(o.task)}</span><span class="dim small">${o.attempts}/${o.max} tries</span></div>`);
    });
    (data.unresolved || []).forEach((u) => {
      items.push(`<div class="srow"><span><b class="dim">FAILED</b> <b>${esc(u.repo)}</b>: ${esc(u.task)} <span class="dim small">[${esc(u.reason)}]</span></span><button class="mini ghost" data-reset-fix="${esc(u.repo)}" data-task="${esc(u.task)}">Retry</button></div>`);
    });
    setHTML(el, items.join('') || '<div class="empty dim">No open or recent fix tasks</div>');
  } catch (e) {
    setHTML(el, '<div class="empty dim">Could not load fix queue</div>');
  }
}

function initFixQueueUI() {
  const btnToggle = $('btnToggleAddFix');
  if (btnToggle && !btnToggle._init) {
    btnToggle._init = true;
    btnToggle.onclick = () => {
      const f = $('addFixForm');
      if (f) f.hidden = !f.hidden;
    };
  }
  const btnSub = $('btnSubmitFixTask');
  if (btnSub && !btnSub._init) {
    btnSub._init = true;
    btnSub.onclick = async () => {
      const repo = $('fixRepoSelect')?.value;
      const task = $('fixTaskInput')?.value?.trim();
      if (!repo || !task) return;
      btnSub.disabled = true;
      try {
        const r = await post('/api/fix-queue', { action: 'add', repo, task });
        if (r?.ok) {
          toast(`Added task to ${repo}`);
          if ($('fixTaskInput')) $('fixTaskInput').value = '';
          const f = $('addFixForm');
          if (f) f.hidden = true;
          loadFixQueue();
        } else {
          toast(r?.why || 'Failed to add task');
        }
      } finally {
        btnSub.disabled = false;
      }
    };
  }
  const list = $('fixQueueList');
  if (list && !list._init) {
    list._init = true;
    list.addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-reset-fix]');
      if (!btn) return;
      const repo = btn.dataset.resetFix;
      const task = btn.dataset.task;
      btn.disabled = true;
      try {
        const r = await post('/api/fix-queue', { action: 'reset', repo, task });
        if (r?.ok) {
          toast(`Reset task for ${repo}`);
          loadFixQueue();
        }
      } finally {
        btn.disabled = false;
      }
    });
  }
}
