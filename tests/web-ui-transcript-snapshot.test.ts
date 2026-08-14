// Only snapshotTranscript() is pinned here: restore needs real innerHTML parsing,
// and a fake elaborate enough to emulate that would be testing the fake.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = readFileSync(join(REPO, 'src', 'web-client.ts'), 'utf-8');

/** Pull the shipped function out of the served page rather than restating it. */
function extract(name: string): string {
  const start = SRC.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `${name}() is gone or renamed — persistence is unguarded`);
  const end = SRC.indexOf('\n}', start);
  assert.notEqual(end, -1, `could not find the end of ${name}()`);
  return SRC.slice(start, end + 2);
}

function constant(name: string): number {
  const m = SRC.match(new RegExp(`const ${name} = (\\d+)`));
  assert.ok(m, `${name} is gone — the bound it enforces is unpinned`);
  return Number(m![1]);
}

const MAX_ENTRIES = constant('TRANSCRIPT_MAX_ENTRIES');
const MAX_ENTRY_LEN = constant('TRANSCRIPT_MAX_ENTRY_LEN');
const KEY = 'sutando-transcript-v1';

/** Minimal element: only the surface snapshotTranscript() actually touches. */
interface CloneStub {
  className: string;
  innerHTML: string;
  querySelectorAll(sel: string): { remove(): void }[];
}
interface ElStub {
  className: string;
  innerHTML: string;
  cloneNode(deep?: boolean): CloneStub;
}

function el(cls: string, html: string, opts: { copyBtn?: boolean } = {}): ElStub {
  const copyBtn = '<span class="copy-btn">Copy</span>';
  const hasCopy = !!opts.copyBtn;
  return {
    className: cls,
    innerHTML: hasCopy ? html + copyBtn : html,
    cloneNode(): CloneStub {
      const c: CloneStub = {
        className: cls,
        innerHTML: hasCopy ? html + copyBtn : html,
        querySelectorAll: (sel: string) => (sel === '.copy-btn' && hasCopy
          ? [{ remove() { c.innerHTML = html; } }] : []),
      };
      return c;
    },
  };
}

function harness(children: ElStub[], { failFirstSet = false } = {}) {
  const store: Record<string, string> = {};
  let sets = 0;
  const localStorage = {
    getItem: (k: string) => (k in store ? store[k] : null),
    setItem: (k: string, v: string) => {
      sets++;
      if (failFirstSet && sets === 1) throw new Error('QuotaExceededError');
      store[k] = v;
    },
  };
  const $ = (id: string) => (id === 'transcript' ? { children } : null);
  const fn = new Function(
    '$', 'localStorage', 'TRANSCRIPT_MAX_ENTRIES', 'TRANSCRIPT_MAX_ENTRY_LEN',
    'PERSIST_KEY_TRANSCRIPT',
    `${extract('snapshotTranscript')}; return snapshotTranscript;`,
  )($, localStorage, MAX_ENTRIES, MAX_ENTRY_LEN, KEY);
  return { run: fn as () => void, stored: () => JSON.parse(store[KEY] || 'null'), sets: () => sets };
}

test('the harness actually drives the shipped function', () => {
  const h = harness([el('t-entry t-user', 'hello')]);
  assert.equal(h.stored(), null, 'storage was primed — a later pass would prove nothing');
  h.run();
  assert.ok(h.stored(), 'snapshotTranscript() wrote nothing; every assertion below would be vacuous');
});

test('entries persist in order with their classes', () => {
  const h = harness([
    el('t-entry t-system', 'seed'),
    el('t-entry t-user', 'one'),
    el('t-entry t-sutando', 'two'),
  ]);
  h.run();
  assert.deepEqual(h.stored(), [
    { cls: 't-entry t-system', html: 'seed' },
    { cls: 't-entry t-user', html: 'one' },
    { cls: 't-entry t-sutando', html: 'two' },
  ]);
});

test('the copy button is stripped — its handler cannot survive an innerHTML round trip', () => {
  const h = harness([el('t-entry t-user', 'ask', { copyBtn: true })]);
  h.run();
  assert.equal(h.stored()[0].html, 'ask', 'a dead copy-btn was persisted into the restored bubble');
});

test('an oversized entry becomes a visible placeholder, never a silent drop', () => {
  const huge = 'x'.repeat(MAX_ENTRY_LEN + 1);
  const h = harness([el('t-entry t-user', 'kept'), el('t-entry t-sutando', huge)]);
  h.run();
  const out = h.stored();
  assert.equal(out.length, 2, 'the oversized entry vanished — the restored transcript would lie');
  assert.match(out[1].html, /not kept across reloads/, 'no placeholder for the dropped content');
  assert.ok(out[1].html.length < MAX_ENTRY_LEN, 'the placeholder is not bounded');
});

test('an empty entry is dropped rather than persisted as a blank bubble', () => {
  const h = harness([el('t-entry t-user', 'real'), el('t-entry t-user', '')]);
  h.run();
  assert.equal(h.stored().length, 1);
});

test('only the most recent TRANSCRIPT_MAX_ENTRIES are kept', () => {
  const many = Array.from({ length: MAX_ENTRIES + 10 }, (_, i) => el('t-entry t-user', `m${i}`));
  const h = harness(many);
  h.run();
  const out = h.stored();
  assert.equal(out.length, MAX_ENTRIES);
  assert.equal(out.at(-1).html, `m${MAX_ENTRIES + 9}`, 'kept the oldest instead of the newest');
});

test('a quota failure retains the most recent half instead of losing the transcript', () => {
  const many = Array.from({ length: 10 }, (_, i) => el('t-entry t-user', `q${i}`));
  const h = harness(many, { failFirstSet: true });
  h.run();
  const out = h.stored();
  assert.ok(out && out.length, 'quota failure wiped the transcript entirely');
  assert.equal(out.length, 5, 'retry did not keep the most recent half');
  assert.equal(out.at(-1).html, 'q9', 'retry kept the oldest half');
  assert.equal(h.sets(), 2, 'expected exactly one retry after the quota failure');
});

test('the observer wiring and the restore guard are still present in the shipped page', () => {
  assert.match(SRC, /new MutationObserver\(scheduleSnapshot\)\.observe\(/,
    'the snapshot observer was removed — appends would stop persisting');
  assert.match(SRC, /if \(_transcriptRestoring\) return;/,
    'the re-entrancy guard is gone — restoring would re-trigger a snapshot of itself');
});
