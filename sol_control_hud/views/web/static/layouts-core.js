// Pure helpers for layouts.js: no DOM, so node can test them (tests/js/layouts_core.test.js, run by tests/test_js.py).
// In the page they are window.LayoutCore; in node, require() returns them.
'use strict';
(function (root) {
  const SIZE_ORDER = ['XS', 'S', 'M', 'L', 'XL'];

  /** The grid span for a card at `size`, never wider than the columns the grid has. */
  function spanFor(sizes, size, cols) {
    const [c, r] = (sizes && sizes[size]) || [1, 1];
    return { cols: Math.max(1, Math.min(c, Math.max(1, cols || 1))), rows: Math.max(1, r || 1) };
  }

  /** The sizes a card supports, smallest first. */
  function sizesOf(sizes) { return SIZE_ORDER.filter((s) => sizes && sizes[s]); }

  /** A copy of `widgets` with card `id` moved to index `to` (clamped). Unknown id: an unchanged copy. */
  function moveTo(widgets, id, to) {
    const ws = widgets.slice();
    const from = ws.findIndex((w) => w.id === id);
    if (from < 0) return ws;
    const [w] = ws.splice(from, 1);
    ws.splice(Math.max(0, Math.min(to, ws.length)), 0, w);
    return ws;
  }

  /** The index the arrow keys move card `id` to: the next visible card in direction `dir` (-1 / +1), or -1. */
  function neighbourIndex(widgets, id, dir) {
    let i = widgets.findIndex((w) => w.id === id) + dir;
    while (i >= 0 && i < widgets.length && widgets[i].hidden) i += dir;
    return i >= 0 && i < widgets.length ? i : -1;
  }

  /** A copy of `widgets` after dropping card `dragId` before (or after) card `overId`. */
  function dropReorder(widgets, dragId, overId, before) {
    if (dragId === overId) return widgets.slice();
    const ws = widgets.slice();
    const from = ws.findIndex((w) => w.id === dragId);
    if (from < 0 || !ws.some((w) => w.id === overId)) return ws;
    const [w] = ws.splice(from, 1);
    const at = ws.findIndex((x) => x.id === overId) + (before ? 0 : 1);
    ws.splice(at, 0, w);
    return ws;
  }

  /** A card shown again from the tray: visible, and moved to the end where you can see it. */
  function showAtEnd(widgets, id) {
    const ws = widgets.map((w) => (w.id === id ? { ...w, hidden: false } : w));
    return moveTo(ws, id, ws.length);
  }

  /** The Disks chip: the drive with the least room (highest percent used). */
  function fullestDisk(disks) {
    return (disks || []).filter((d) => d && d.free_gb != null).sort((a, b) => (b.percent || 0) - (a.percent || 0))[0] || null;
  }

  const api = { SIZE_ORDER, spanFor, sizesOf, moveTo, neighbourIndex, dropReorder, showAtEnd, fullestDisk };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.LayoutCore = api;
}(typeof window !== 'undefined' ? window : globalThis));
