/**
 * The proxy writes `limit_state` into quota-state.json so the desktop and web
 * clients stop re-deriving it: a core serving on extra usage (overage) reads
 * `overage` + available, not a usage-limit pause (issue #5283). The rule is the
 * Python `classify_limit_state`; both run the shared parity table.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { limitState, quotaStateFromHeaders, recordWithRejection } from '../skills/quota-tracker/scripts/credential-proxy.ts';

const fixture = JSON.parse(
	readFileSync(new URL('./fixtures/quota-limit-state.parity.json', import.meta.url), 'utf8'),
) as { limitState: Array<{ name: string; record: unknown; expect: string }> };

const P = 'anthropic-ratelimit-unified-';
const INCIDENT = {
	[`${P}status`]: 'rejected', [`${P}7d-status`]: 'rejected', [`${P}7d-utilization`]: '1.0',
	[`${P}5h-status`]: 'allowed', [`${P}overage-status`]: 'allowed',
};
const T0 = '2026-10-09T12:00:00.000Z';

test('limitState matches every row of the shared parity table', () => {
	for (const row of fixture.limitState) assert.equal(limitState(row.record), row.expect, row.name);
});

test('a header write on extra usage records overage and stays available', () => {
	const s = quotaStateFromHeaders({}, INCIDENT, 'claude-opus-5-5', T0);
	assert.equal(s.limit_state, 'overage');
	assert.equal(s.available, true);
	assert.equal('exhausted_since' in s, false);
});

test('a header write on a hard limit is unchanged: rejected and unavailable', () => {
	const s = quotaStateFromHeaders({}, { ...INCIDENT, [`${P}overage-status`]: 'rejected' }, '', T0);
	assert.equal(s.limit_state, 'rejected');
	assert.equal(s.available, false);
	assert.equal(s.exhausted_since, T0);
});

test('an allowed header write records allowed', () => {
	const s = quotaStateFromHeaders({}, { [`${P}status`]: 'allowed', [`${P}5h-status`]: 'allowed' }, '', T0);
	assert.equal(s.limit_state, 'allowed');
	assert.equal(s.available, true);
});

test('a 429 landing after an overage write flips it to rejected', () => {
	const prev = quotaStateFromHeaders({}, INCIDENT, '', T0);
	const next = recordWithRejection(prev, { ts: '2026-10-09T12:00:01.000Z', status: 429, path: '/v1/messages', snippet: '' });
	assert.equal(next.limit_state, 'rejected');
	assert.equal(next.available, false);
});

test('a non-429 rejection leaves an overage reading alone', () => {
	const prev = quotaStateFromHeaders({}, INCIDENT, '', T0);
	const next = recordWithRejection(prev, { ts: '2026-10-09T12:00:01.000Z', status: 529, path: '/v1/messages', snippet: '' });
	assert.equal(next.limit_state, 'overage');
	assert.equal(next.available, true);
});

test('the next successful write after a 429 returns to overage', () => {
	const after429 = recordWithRejection(quotaStateFromHeaders({}, INCIDENT, '', T0),
		{ ts: '2026-10-09T12:00:01.000Z', status: 429, path: '/v1/messages', snippet: '' });
	const s = quotaStateFromHeaders(after429, INCIDENT, '', '2026-10-09T12:00:02.000Z');
	assert.equal(s.limit_state, 'overage');
	assert.equal(s.available, true);
});
