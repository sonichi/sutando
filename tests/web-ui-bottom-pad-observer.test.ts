// The harness supplies its own ResizeObserver and positively controls that it can
// deliver, because a hidden automation tab fires no observer callbacks at all.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = readFileSync(join(REPO, 'src', 'web-client.ts'), 'utf-8');

const PAD_SLACK_PX = 12;

/** Pull the shipped function out of the served page rather than restating it. */
function extractSyncBottomPad(): string {
  const start = SRC.indexOf('function syncBottomPad()');
  assert.notEqual(start, -1, 'syncBottomPad() is gone or renamed — clearance is unguarded');
  const end = SRC.indexOf('\n}', start);
  assert.notEqual(end, -1, 'could not find the end of syncBottomPad()');
  return SRC.slice(start, end + 2);
}

function harness() {
  const panel = { offsetHeight: 150 };
  const body = { style: { paddingBottom: '' } };
  const doc = { body };
  const $ = (id: string) => (id === 'bottom-panel' ? panel : null);

  let observerCb: (() => void) | null = null;
  let observed: unknown = null;
  class FakeResizeObserver {
    constructor(cb: () => void) { observerCb = cb; }
    observe(el: unknown) { observed = el; }
    disconnect() { observerCb = null; }
  }

  const syncBottomPad = new Function(
    'document', '$', `${extractSyncBottomPad()}; return syncBottomPad;`,
  )(doc, $) as () => void;

  return {
    panel, body,
    wire() { new FakeResizeObserver(syncBottomPad).observe(panel); syncBottomPad(); },
    fireResize() {
      assert.ok(observerCb, 'no observer callback registered — instrument cannot deliver');
      observerCb!();
    },
    observedEl: () => observed,
    pad: () => body.style.paddingBottom,
  };
}

test('the instrument can actually deliver a resize observation', () => {
  const h = harness();
  h.wire();
  assert.equal(h.observedEl(), h.panel, 'observer was never attached to #bottom-panel');
  let delivered = 0;
  h.panel.offsetHeight = 151;
  h.fireResize();
  if (h.pad() === '163px') delivered++;
  assert.equal(delivered, 1,
    'the fake observer did not reach syncBottomPad — every later assertion would be vacuous');
});

test('padding tracks the panel height as it grows', () => {
  const h = harness();
  h.wire();
  assert.equal(h.pad(), `${150 + PAD_SLACK_PX}px`, 'initial sync did not run');

  for (const height of [220, 320, 480]) {
    h.panel.offsetHeight = height;
    h.fireResize();
    assert.equal(h.pad(), `${height + PAD_SLACK_PX}px`,
      `padding did not track a grow to ${height}`);
  }
});

test('without the observation the padding goes stale — the assertion is sensitive', () => {
  const h = harness();
  h.wire();
  h.panel.offsetHeight = 480;
  assert.equal(h.pad(), `${150 + PAD_SLACK_PX}px`,
    'padding changed without any observation, so the grow assertions prove nothing');
});

test('the ResizeObserver wiring is still present in the shipped page', () => {
  assert.match(SRC, /new ResizeObserver\(syncBottomPad\)\.observe\(bp\)/,
    'the observer wiring was removed — padding would only update on window resize');
  assert.match(SRC, /window\.addEventListener\('resize', syncBottomPad\)/,
    'the resize fallback was removed');
});
