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
