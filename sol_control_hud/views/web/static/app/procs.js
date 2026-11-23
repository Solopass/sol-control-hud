// Process Radar (split from app.js)
'use strict';
// ---------- Process Radar (#c-procs)
let procSortMode = 'cpu';

async function loadProcs() {
  if (document.hidden) return;
  if (typeof cardShown === 'function' && !cardShown('c-procs')) return;
  try {
    const d = await (await fetch('/api/top-processes', { cache: 'no-store' })).json();
    if (!d.ok) return;
    renderProcTable(d);
  } catch (e) { /* ignore */ }
}

function renderProcTable(data) {
  const tbody = $('procRows');
  if (!tbody) return;
  const list = procSortMode === 'mem' ? data.top_mem : procSortMode === 'gpu' ? data.top_gpu : data.top_cpu;
  if (!list || !list.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty">No processes recorded</td></tr>';
    return;
  }
  tbody.innerHTML = list.map((p) => {
    const chip = p.chip ? `<span class="dim" style="font-size:10px">${esc(p.chip)}</span>` : '<span class="dim">–</span>';
    return `<tr>` +
      `<td class="dim">${p.pid}</td>` +
      `<td class="proc-name-cell" title="${esc(p.name)}"><b>${esc(p.name)}</b></td>` +
      `<td class="num" style="color:${p.cpu > 25 ? 'var(--amber)' : 'inherit'}">${p.cpu}%</td>` +
      `<td class="num">${Math.round(p.mem_mb)} MB</td>` +
      `<td>${chip}</td>` +
      `<td class="act"><button class="mini danger ghost" data-kill-pid="${p.pid}" data-proc-name="${esc(p.name)}" title="End task">✕</button></td>` +
      `</tr>`;
  }).join('');
}

function initProcs() {
  const table = $('procTable');
  if (table && !table._inited) {
    table._inited = true;
    table.addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-kill-pid]');
      if (!btn) return;
      const pid = parseInt(btn.dataset.killPid, 10);
      const name = btn.dataset.procName || 'process';
      if (!(await hudConfirm(`End process ${name} (PID ${pid})?`))) return;
      const r = await post('/api/action', { action: 'kill_process', target: String(pid) });
      if (r?.ok) {
        toast(`Ended ${name}`);
        loadProcs();
      } else {
        toast(r?.why || 'Could not end process');
      }
    });
  }

  ['procSortCpu', 'procSortMem', 'procSortGpu'].forEach((id) => {
    const btn = $(id);
    if (!btn) return;
    btn.onclick = () => {
      ['procSortCpu', 'procSortMem', 'procSortGpu'].forEach((bId) => {
        const b = $(bId);
        if (b) {
          b.classList.remove('active');
          b.classList.add('ghost');
        }
      });
      btn.classList.add('active');
      btn.classList.remove('ghost');
      procSortMode = btn.dataset.sort;
      loadProcs();
    };
  });

  const refreshBtn = $('procRefresh');
  if (refreshBtn) refreshBtn.onclick = loadProcs;
}

