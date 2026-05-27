import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

// Point conversation-store at a throwaway DB before importing.
const TMP_WS = mkdtempSync(join(tmpdir(), 'sutando-convstore-'));
process.env.SUTANDO_CONVERSATION_DB = join(TMP_WS, 'data', 'conversation.sqlite');

const {
	recordConversation,
	recordSession,
	recordSessionBoundary,
} = await import('../src/conversation-store.js');

// tsToUnix is not exported — exercise through recordSession (which uses it
// internally when fan-ning out toolCalls/events timestamps).

describe('conversation-store — recordConversation', () => {
	it('does not throw on valid role + text', () => {
		assert.doesNotThrow(() => recordConversation('user', 'hello'));
	});

	it('does not throw with optional sessionId', () => {
		assert.doesNotThrow(() => recordConversation('assistant', 'hi', 'sess-1'));
	});

	it('does not throw on empty text', () => {
		assert.doesNotThrow(() => recordConversation('user', ''));
	});
});

describe('conversation-store — recordSessionBoundary', () => {
	it('does not throw with default reason', () => {
		assert.doesNotThrow(() => recordSessionBoundary());
	});

	it('does not throw with explicit reason', () => {
		assert.doesNotThrow(() => recordSessionBoundary('session_timeout', 'sess-2'));
	});
});

describe('conversation-store — recordSession', () => {
	it('does not throw for minimal metrics', () => {
		assert.doesNotThrow(() =>
			recordSession({ source: 'voice', durationMs: 1234 })
		);
	});

	it('accepts all optional fields', () => {
		assert.doesNotThrow(() =>
			recordSession({
				source: 'phone',
				sessionId: 'sid-1',
				callSid: 'CA123',
				caller: '+1555000',
				isOwner: true,
				isMeeting: false,
				durationMs: 60000,
				transcriptLines: 42,
				toolCount: 3,
				pendingTasks: 1,
			})
		);
	});

	it('fans out toolCalls array without throwing', () => {
		assert.doesNotThrow(() =>
			recordSession({
				source: 'voice',
				sessionId: 'sid-2',
				durationMs: 5000,
				toolCalls: [
					{ name: 'describe_screen', durationMs: 120, timestamp: Date.now() / 1000 },
					{ name: 'work', durationMs: 2000, timestamp: '2026-01-01T00:00:00Z' },
				],
			})
		);
	});

	it('fans out events array without throwing', () => {
		assert.doesNotThrow(() =>
			recordSession({
				source: 'discord-voice',
				durationMs: 3000,
				events: [
					{ event: 'session_started', timestamp: Date.now() / 1000 },
					{ event: 'call_ended', timestamp: '2026-05-01T10:00:00.000Z' },
				],
			})
		);
	});

	it('handles null / undefined optional fields', () => {
		assert.doesNotThrow(() =>
			recordSession({
				source: 'phone',
				sessionId: null,
				callSid: null,
				caller: null,
				isOwner: null,
				isMeeting: null,
				durationMs: 0,
				transcriptLines: null,
				toolCount: null,
				pendingTasks: null,
			})
		);
	});

	it('silently skips non-array toolCalls', () => {
		assert.doesNotThrow(() =>
			recordSession({
				source: 'voice',
				durationMs: 1000,
				// @ts-expect-error intentional bad type
				toolCalls: 'not-an-array',
			})
		);
	});

	it('handles toolCalls entries with missing name/timestamp gracefully', () => {
		assert.doesNotThrow(() =>
			recordSession({
				source: 'voice',
				durationMs: 1000,
				toolCalls: [{ durationMs: 50 }, {}],
			})
		);
	});

	it('handles events entries with no timestamp (falls back to now)', () => {
		assert.doesNotThrow(() =>
			recordSession({
				source: 'phone',
				durationMs: 2000,
				events: [{ event: 'session_started' }],
			})
		);
	});
});

describe('conversation-store — tsToUnix edge cases (via recordSession fan-out)', () => {
	it('accepts millisecond-scale numeric timestamp in toolCalls', () => {
		assert.doesNotThrow(() =>
			recordSession({
				source: 'voice',
				durationMs: 1000,
				toolCalls: [{ name: 'x', timestamp: 1_700_000_000_000 }],
			})
		);
	});

	it('accepts second-scale numeric timestamp in toolCalls', () => {
		assert.doesNotThrow(() =>
			recordSession({
				source: 'voice',
				durationMs: 1000,
				toolCalls: [{ name: 'x', timestamp: 1_700_000_000 }],
			})
		);
	});

	it('accepts ISO-8601 string timestamp in events', () => {
		assert.doesNotThrow(() =>
			recordSession({
				source: 'voice',
				durationMs: 1000,
				events: [{ event: 'e', timestamp: '2026-05-24T12:00:00.000Z' }],
			})
		);
	});

	it('handles garbage timestamp string gracefully', () => {
		assert.doesNotThrow(() =>
			recordSession({
				source: 'voice',
				durationMs: 1000,
				toolCalls: [{ name: 'x', timestamp: 'not-a-date' }],
			})
		);
	});
});

after(() => {
	try { rmSync(TMP_WS, { recursive: true, force: true }); } catch { /* ignore */ }
});
