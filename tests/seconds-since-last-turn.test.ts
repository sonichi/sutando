import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

/**
 * Regression test for PR #515. The original `getSecondsSinceLastTurn()`
 * read the last line regardless of role, so a `core-agent` task-result
 * line written by the result watcher between user turns made a
 * long-away user look like a quick reconnect — and the "Welcome back"
 * was wrongly suppressed. Fix: walk backward, skip non-dialogue lines,
 * stop at SESSION_END.
 *
 * The function under test is replicated inline rather than imported —
 * task-bridge has module-load side effects (mirrors the convention
 * used by session-boundary.test.ts).
 */
function secondsSinceLastTurn(content: string, now: number): number | null {
	if (!content) return null;
	const lines = content.split('\n');
	for (let i = lines.length - 1; i >= 0; i--) {
		const role = lines[i].split('|')[1];
		if (role === 'SESSION_END') return null;
		if (role !== 'user' && role !== 'assistant') continue;
		const ts = Date.parse(lines[i].split('|')[0]);
		if (Number.isNaN(ts)) return null;
		return (now - ts) / 1000;
	}
	return null;
}

describe('getSecondsSinceLastTurn — role-filtered', () => {
	const now = Date.parse('2026-04-25T10:10:00Z');

	it('returns null on empty log', () => {
		assert.equal(secondsSinceLastTurn('', now), null);
	});

	it('returns gap from the most recent user turn', () => {
		const log = '2026-04-25T10:09:30Z|user|hi'; // 30s ago
		assert.equal(secondsSinceLastTurn(log, now), 30);
	});

	it('returns gap from the most recent assistant turn', () => {
		const log = '2026-04-25T10:09:00Z|assistant|sure'; // 60s ago
		assert.equal(secondsSinceLastTurn(log, now), 60);
	});

	it('skips core-agent task-result lines (the bug sonichi found)', () => {
		// User was away 5 minutes (300s). Proactive loop wrote a
		// core-agent result 30s ago. Old impl returned 30 → quick
		// reconnect → "Welcome back" wrongly suppressed. New impl
		// must skip the core-agent line and return ~300s.
		const log = [
			'2026-04-25T10:05:00Z|user|brb',                       // 300s ago
			'2026-04-25T10:09:30Z|core-agent|[task:abc] done',     // 30s ago
		].join('\n');
		assert.equal(secondsSinceLastTurn(log, now), 300);
	});

	it('returns null after SESSION_END (current session has no turns yet)', () => {
		// Session ended cleanly — don't reach back into the prior
		// session for "last turn" purposes.
		const log = [
			'2026-04-25T09:00:00Z|user|first session',
			'2026-04-25T09:00:05Z|SESSION_END|user_goodbye',
		].join('\n');
		assert.equal(secondsSinceLastTurn(log, now), null);
	});

	it('returns gap from a turn after SESSION_END (new session in progress)', () => {
		const log = [
			'2026-04-25T09:00:00Z|user|first session',
			'2026-04-25T09:00:05Z|SESSION_END|user_goodbye',
			'2026-04-25T10:09:30Z|user|new session',  // 30s ago
		].join('\n');
		assert.equal(secondsSinceLastTurn(log, now), 30);
	});

	it('skips multiple core-agent lines and finds the user turn', () => {
		const log = [
			'2026-04-25T10:05:00Z|user|question',                    // 300s ago
			'2026-04-25T10:06:00Z|core-agent|[task:1] result',
			'2026-04-25T10:07:00Z|core-agent|[task:2] result',
			'2026-04-25T10:09:30Z|core-agent|[task:3] result',
		].join('\n');
		assert.equal(secondsSinceLastTurn(log, now), 300);
	});
});
