// node --test: the pure layout logic the dashboard uses (sol_control_hud/views/web/static/layouts-core.js).
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const LC = require(path.join(__dirname, '..', '..', 'sol_control_hud', 'views', 'web', 'static', 'layouts-core.js'));

const W = (ids, hidden = []) => ids.map((id) => ({ id, size: 'M', hidden: hidden.includes(id) }));
const ids = (ws) => ws.map((w) => w.id).join(' ');

test('a span never exceeds the columns the grid has', () => {
  const sizes = { XS: [1, 1], L: [2, 1], XL: [3, 2] };
  assert.deepEqual(LC.spanFor(sizes, 'XL', 4), { cols: 3, rows: 2 });
  assert.deepEqual(LC.spanFor(sizes, 'XL', 2), { cols: 2, rows: 2 });
  assert.deepEqual(LC.spanFor(sizes, 'L', 1), { cols: 1, rows: 1 });     // a phone-width grid
  assert.deepEqual(LC.spanFor(sizes, 'M', 4), { cols: 1, rows: 1 });     // a size the card doesn't have
  assert.deepEqual(LC.spanFor(sizes, 'L', 0), { cols: 1, rows: 1 });     // no columns measured yet
});

test('sizes come smallest first', () => {
  assert.deepEqual(LC.sizesOf({ XL: [3, 1], XS: [1, 1], M: [1, 1] }), ['XS', 'M', 'XL']);
  assert.deepEqual(LC.sizesOf(undefined), []);
});

test('moving a card returns a new order and leaves the old one alone', () => {
  const ws = W(['a', 'b', 'c', 'd']);
  assert.equal(ids(LC.moveTo(ws, 'd', 0)), 'd a b c');
  assert.equal(ids(LC.moveTo(ws, 'a', 99)), 'b c d a');                  // clamped to the end
  assert.equal(ids(LC.moveTo(ws, 'nope', 0)), 'a b c d');
  assert.equal(ids(ws), 'a b c d');
});

test('the arrow keys skip hidden cards and stop at the ends', () => {
  const ws = W(['a', 'b', 'c', 'd'], ['b', 'c']);
  assert.equal(LC.neighbourIndex(ws, 'a', 1), 3);                        // jumps over the two hidden ones
  assert.equal(LC.neighbourIndex(ws, 'd', -1), 0);
  assert.equal(LC.neighbourIndex(ws, 'a', -1), -1);
  assert.equal(LC.neighbourIndex(ws, 'd', 1), -1);
});

test('dropping a card before or after another', () => {
  const ws = W(['a', 'b', 'c', 'd']);
  assert.equal(ids(LC.dropReorder(ws, 'a', 'c', true)), 'b a c d');
  assert.equal(ids(LC.dropReorder(ws, 'a', 'c', false)), 'b c a d');
  assert.equal(ids(LC.dropReorder(ws, 'd', 'a', true)), 'd a b c');
  assert.equal(ids(LC.dropReorder(ws, 'b', 'b', true)), 'a b c d');      // onto itself
  assert.equal(ids(LC.dropReorder(ws, 'x', 'a', true)), 'a b c d');      // unknown card
});

test('a card brought back from the tray shows at the end', () => {
  const ws = LC.showAtEnd(W(['a', 'b', 'c'], ['a']), 'a');
  assert.equal(ids(ws), 'b c a');
  assert.equal(ws[2].hidden, false);
});

test('the Disks chip picks the fullest drive', () => {
  const d = LC.fullestDisk([{ drive: 'C', free_gb: 766, percent: 18 }, { drive: 'E', free_gb: 300, percent: 90 },
    { drive: 'D', error: 'unavailable' }]);
  assert.equal(d.drive, 'E');
  assert.equal(LC.fullestDisk([]), null);
});
