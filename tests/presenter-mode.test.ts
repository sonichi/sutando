// Contract tests for the TS presenter-mode sentinel policy — mirrors the case
// table of tests/presenter-mode-policy.test.py so the two language twins
// cannot drift apart silently (#2501 follow-up).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { presenterModeActive } from '../src/presenter-mode.js';

const EPOCH = '1970-01-01T00:00:00Z';

function workspaceWithSentinel(content: string | null): string {
  const ws = mkdtempSync(join(tmpdir(), 'sutando-presenter-ts-'));
  mkdirSync(join(ws, 'state'), { recursive: true });
  if (content !== null) {
    writeFileSync(join(ws, 'state', 'presenter-mode.sentinel'), content);
  }
  return ws;
}

test('missing sentinel is inactive', () => {
  assert.equal(presenterModeActive(workspaceWithSentinel(null), EPOCH), false);
});

test('empty sentinel is inactive', () => {
  assert.equal(presenterModeActive(workspaceWithSentinel(''), EPOCH), false);
});

test('malformed sentinel is inactive', () => {
  assert.equal(presenterModeActive(workspaceWithSentinel('garbage'), EPOCH), false);
});

test('digit-prefixed malformed sentinel is inactive — fail-closed (#2516 review canary)', () => {
  // '9999-not-a-date' starts with a digit and lexically compares as future;
  // a first-byte check plus raw compare reads it as ACTIVE. The documented
  // contract says malformed fails closed, so the full UTC shape must validate.
  assert.equal(presenterModeActive(workspaceWithSentinel('9999-not-a-date'), EPOCH), false);
});

// Shape-valid but SEMANTICALLY impossible (#2516 second review round). The
// full-shape regex pins field widths only, so each of these matched and then
// lexically compared as future — holding the gate open forever on a corrupted
// sentinel. Mirrored case-for-case in tests/presenter-mode-policy.test.py.
//
// The rollover cases are why a bare parse is not enough HERE specifically:
// `new Date('2026-02-30T00:00:00Z')` does NOT throw, it means 2026-03-02. The
// Python twin's strptime rejects it outright, so without the canonical
// round-trip the two implementations would disagree on exactly these values.
for (const [value, why] of [
  ['9999-99-99T99:99:99Z', 'impossible in every field'],
  ['2026-13-01T00:00:00Z', 'month 13'],
  ['2026-00-01T00:00:00Z', 'month 00'],
  ['2026-01-32T00:00:00Z', 'day 32'],
  ['2027-01-01T24:00:00Z', 'hour 24'],
  ['2027-02-30T00:00:00Z', 'Feb 30 — Date rolls it over rather than failing'],
  ['2027-06-31T00:00:00Z', 'Jun 31 — Date rolls it over rather than failing'],
  ['2027-02-29T00:00:00Z', 'Feb 29 in a non-leap year'],
] as const) {
  test(`shape-valid but impossible sentinel is inactive: ${value} (${why})`, () => {
    assert.equal(presenterModeActive(workspaceWithSentinel(value), EPOCH), false);
  });
}

// CONTROLS — without these the fix could pass by rejecting everything, which
// would silently disable presenter mode instead of hardening it.
test('CONTROL: a real leap day is still ACTIVE', () => {
  assert.equal(presenterModeActive(workspaceWithSentinel('2028-02-29T00:00:00Z'), EPOCH), true);
});

test('CONTROL: a genuinely future expiry is still ACTIVE', () => {
  assert.equal(presenterModeActive(workspaceWithSentinel('2099-12-31T23:59:59Z'), EPOCH), true);
});

test('future expiry is active', () => {
  assert.equal(presenterModeActive(workspaceWithSentinel('1970-01-01T00:00:01Z\n'), EPOCH), true);
});

test('expiry is exclusive', () => {
  assert.equal(presenterModeActive(workspaceWithSentinel('1970-01-01T00:00:00Z'), EPOCH), false);
});

test('past expiry is inactive — the regression this file exists for', () => {
  // A talk window that lapsed without `presenter-mode.sh stop` leaves this
  // exact on-disk state; existence-only readers stay gated forever.
  assert.equal(presenterModeActive(workspaceWithSentinel('1969-12-31T23:59:59Z'), EPOCH), false);
});

test('unreadable sentinel fails closed', () => {
  const ws = workspaceWithSentinel(null);
  // A directory at the sentinel path makes readFileSync throw (EISDIR).
  mkdirSync(join(ws, 'state', 'presenter-mode.sentinel'));
  assert.equal(presenterModeActive(ws, EPOCH), false);
  rmSync(join(ws, 'state', 'presenter-mode.sentinel'), { recursive: true });
});
