import { describe, it, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

/**
 * Tests for the session-boundary trimming logic added in PR #257
 * commit 14 (ad32bbc). Replaces the fragile pattern-match filter
 * with a correct-by-construction sentinel-marker approach:
 * `getRecentConversation()` scans backward for the most recent
 * `|SESSION_END|` marker and returns only lines AFTER it.
 *
 * The function under test is replicated here inline rather than
 * importing from task-bridge.ts. task-bridge has module-load
 * side effects (directory creation, env reads) that the existing
 * tests avoid the same way (see twilio-signature.test.ts,
 * end-session-gate.test.ts).
 */

type ReadFile = (path: string) => string;

function parseRecentConversation(content: string, count: number): string {
	const allLines = content.trim().split('\n');
	let lastBoundary = -1;
	for (let i = allLines.length - 1; i >= 0; i--) {
		if (allLines[i].includes('|SESSION_END|')) {
			lastBoundary = i;
			break;
		}
	}
	const currentSession = lastBoundary >= 0 ? allLines.slice(lastBoundary + 1) : allLines;
	const lines = currentSession.slice(-count);
	return lines.map(l => {
		const [, role, text] = l.split('|', 3);
		return role && text ? `${role}: ${text}` : '';
	}).filter(Boolean).join('\n');
}

describe('session-boundary conversation trimming', () => {
	it('returns empty when the log is empty', () => {
		assert.equal(parseRecentConversation('', 8), '');
	});

	it('returns all lines when there is no boundary marker', () => {
		const log = [
			'2026-04-09T10:00:00Z|user|hi',
			'2026-04-09T10:00:02Z|assistant|hello',
		].join('\n');
		assert.equal(parseRecentConversation(log, 8), 'user: hi\nassistant: hello');
	});

	it('returns empty when the log ends with a boundary marker', () => {
		const log = [
			'2026-04-09T10:00:00Z|user|hi',
			'2026-04-09T10:00:02Z|assistant|hello',
			'2026-04-09T10:00:05Z|SESSION_END|user_goodbye',
		].join('\n');
		assert.equal(parseRecentConversation(log, 8), '');
	});

	it('returns only lines after the most recent boundary marker', () => {
		const log = [
			'2026-04-09T10:00:00Z|user|old_session_q',
			'2026-04-09T10:00:02Z|assistant|Goodbye. Ending session.',
			'2026-04-09T10:00:05Z|SESSION_END|user_goodbye',
			'2026-04-09T10:05:00Z|user|hi again',
			'2026-04-09T10:05:02Z|assistant|hello',
		].join('\n');
		const result = parseRecentConversation(log, 8);
		assert.equal(result, 'user: hi again\nassistant: hello');
		// Critical: "Goodbye. Ending session." from the old session
		// must NOT appear — that's the whole point of the marker.
		assert.ok(!result.includes('Goodbye'));
		assert.ok(!result.includes('Ending session'));
	});

	it('uses the LAST boundary when there are multiple', () => {
		// Two sessions, two end markers, a third session in progress
		const log = [
			'2026-04-09T10:00:00Z|user|session1',
			'2026-04-09T10:00:05Z|SESSION_END|user_goodbye',
			'2026-04-09T10:30:00Z|user|session2',
			'2026-04-09T10:30:02Z|assistant|Goodbye. Farewell.',
			'2026-04-09T10:30:05Z|SESSION_END|user_goodbye',
			'2026-04-09T11:00:00Z|user|session3_in_progress',
		].join('\n');
		const result = parseRecentConversation(log, 8);
		assert.equal(result, 'user: session3_in_progress');
		assert.ok(!result.includes('Farewell'));
		assert.ok(!result.includes('session1'));
		assert.ok(!result.includes('session2'));
	});

	it('respects the count limit', () => {
		// 10 lines, no boundary, count=3 should return the last 3
		const lines = [];
		for (let i = 0; i < 10; i++) {
			lines.push(`2026-04-09T10:00:0${i}Z|user|message${i}`);
		}
		const log = lines.join('\n');
		const result = parseRecentConversation(log, 3);
		assert.equal(result, 'user: message7\nuser: message8\nuser: message9');
	});

	it('respects the count limit within a bounded session', () => {
		// 5 lines after boundary, count=3 should return the last 3
		const log = [
			'2026-04-09T09:00:00Z|user|old',
			'2026-04-09T09:00:05Z|SESSION_END|user_goodbye',
			'2026-04-09T10:00:00Z|user|new1',
			'2026-04-09T10:00:01Z|assistant|resp1',
			'2026-04-09T10:00:02Z|user|new2',
			'2026-04-09T10:00:03Z|assistant|resp2',
			'2026-04-09T10:00:04Z|user|new3',
		].join('\n');
		const result = parseRecentConversation(log, 3);
		assert.equal(result, 'assistant: resp2\nuser: new3\nassistant: resp1\nuser: new2\nassistant: resp2\nuser: new3'.split('\n').slice(-3).join('\n'));
	});

	it('ignores goodbye-like text in the content field', () => {
		// A user message that contains "goodbye" as content, NOT a
		// session-end marker, should NOT be treated as a boundary.
		// This is the case the old pattern filter got wrong.
		const log = [
			'2026-04-09T10:00:00Z|user|say goodbye to the old way',
			'2026-04-09T10:00:02Z|assistant|Sure, what would you like to do instead?',
		].join('\n');
		const result = parseRecentConversation(log, 8);
		assert.ok(result.includes('say goodbye'));
		assert.ok(result.includes('what would you like to do'));
	});

	it('handles SESSION_END marker with various reason fields', () => {
		const log = [
			'2026-04-09T10:00:00Z|user|test',
			'2026-04-09T10:00:05Z|SESSION_END|retroactive_cleanup',
			'2026-04-09T10:05:00Z|user|after',
		].join('\n');
		const result = parseRecentConversation(log, 8);
		assert.equal(result, 'user: after');
	});
});
