// buttons and clickable cards (split from app.js)
'use strict';
// ---------- buttons and clickable cards
document.addEventListener('click', async (e) => {
  const b = e.target.closest('[data-act]');
  if (b) {
    e.stopPropagation();
    if (b.dataset.act === 'review' && b.dataset.target === 'brave_search') {
      const c = lastReviewState && lastReviewState.current;
      if (c) {
        const text = formatVerificationText(c);
        if (text && navigator.clipboard) {
          navigator.clipboard.writeText(text).catch(() => {});
        }
        const q = buildBraveAiQuery(c);
        if (q) {
          window.open('https://search.brave.com/search?q=' + encodeURIComponent(q), '_blank');
        }
        toast('Copied to clipboard & opened Brave AI');
        post('/api/action', { action: 'review', target: 'brave_search', client_handled: true });
        return;
      }
    }
    if (b.dataset.confirm && !(await hudConfirm(b.dataset.confirm))) return;
    const r = await post('/api/action', { action: b.dataset.act, target: b.dataset.target || '' });
    if (r.why) toast(r.why);
    return;
  }
  const card = e.target.closest('.card[data-open]');
  if (card && !e.target.closest('button, a, select')) post('/api/action', { action: 'open', target: card.dataset.open });
});
$('stopAi').onclick = async () => {
  if (!(await hudConfirm('Stop the Away work now? The running job goes back in the queue (finished work is kept).'))) return;
  const r = await post('/api/action', { action: 'stop_ai' }); toast(r.why || 'stopping');
};
$('theme').onchange = (e) => { applyTheme(e.target.value); post('/api/action', { action: 'theme', target: e.target.value }); };
const revHudCb = $('revToggleHud');
if (revHudCb) revHudCb.onchange = (e) => { post('/api/action', { action: 'review', target: 'toggle_hud', enabled: e.target.checked }); };

