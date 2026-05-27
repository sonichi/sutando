import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

const { injectText } = await import('../src/browser-tools.js');
const {
	demoStateRef,
	narrationSpeakingRef,
	lastSpokenRef,
	nextDescRef,
	scrollPausedRef,
} = await import('../src/recording-state.js');

// ── injectText ────────────────────────────────────────────────────────────────
// Pure branch dispatch — calls the first available injection method on the
// session transport. No I/O or macOS dependencies; fully mockable.

describe('injectText — sendRealtimeInput path', () => {
	it('calls sendRealtimeInput with {text} when available', () => {
		const calls: { text: string }[] = [];
		const session = {
			transport: {
				session: {
					sendRealtimeInput: (arg: { text: string }) => calls.push(arg),
				},
			},
		};
		injectText(session, 'hello world');
		assert.equal(calls.length, 1);
		assert.deepEqual(calls[0], { text: 'hello world' });
	});

	it('prefers sendRealtimeInput over sendContent when both available', () => {
		const realtimeCalls: unknown[] = [];
		const contentCalls: unknown[] = [];
		const session = {
			transport: {
				session: {
					sendRealtimeInput: (arg: unknown) => realtimeCalls.push(arg),
				},
				sendContent: (arg: unknown) => contentCalls.push(arg),
			},
		};
		injectText(session, 'priority test');
		assert.equal(realtimeCalls.length, 1);
		assert.equal(contentCalls.length, 0);
	});
});

describe('injectText — sendContent path', () => {
	it('calls sendContent when sendRealtimeInput is absent', () => {
		const calls: [unknown, unknown][] = [];
		const session = {
			transport: {
				sendContent: (parts: unknown, flag: unknown) => calls.push([parts, flag]),
			},
		};
		injectText(session, 'fallback text');
		assert.equal(calls.length, 1);
		const [parts, flag] = calls[0];
		assert.deepEqual(parts, [{ role: 'user', text: 'fallback text' }]);
		assert.equal(flag, true);
	});

	it('sendContent receives the user role and exact text', () => {
		const received: Array<{ role: string; text: string }[]> = [];
		const session = {
			transport: {
				sendContent: (parts: { role: string; text: string }[]) => received.push(parts),
			},
		};
		injectText(session, 'exact text value');
		assert.equal(received[0][0].role, 'user');
		assert.equal(received[0][0].text, 'exact text value');
	});
});

describe('injectText — no injection method', () => {
	it('does not throw when session has no injection method', () => {
		const session = { transport: {} };
		assert.doesNotThrow(() => injectText(session, 'no-op text'));
	});

	it('does not throw when session is null', () => {
		assert.doesNotThrow(() => injectText(null, 'null session'));
	});

	it('does not throw when session is undefined', () => {
		assert.doesNotThrow(() => injectText(undefined, 'undefined session'));
	});

	it('does not throw when transport is null', () => {
		assert.doesNotThrow(() => injectText({ transport: null }, 'null transport'));
	});
});

describe('injectText — error handling', () => {
	it('does not throw when sendRealtimeInput throws', () => {
		const session = {
			transport: {
				session: {
					sendRealtimeInput: () => { throw new Error('network error'); },
				},
			},
		};
		assert.doesNotThrow(() => injectText(session, 'error test'));
	});

	it('does not throw when sendContent throws', () => {
		const session = {
			transport: {
				sendContent: () => { throw new Error('send failed'); },
			},
		};
		assert.doesNotThrow(() => injectText(session, 'send error test'));
	});
});

// ── recording-state module ────────────────────────────────────────────────────

describe('recording-state — initial values', () => {
	it('demoStateRef.value starts as "idle"', () => {
		assert.equal(demoStateRef.value, 'idle');
	});

	it('narrationSpeakingRef.value starts as false', () => {
		assert.equal(narrationSpeakingRef.value, false);
	});

	it('lastSpokenRef.value starts as empty string', () => {
		assert.equal(lastSpokenRef.value, '');
	});

	it('nextDescRef.value starts as null', () => {
		assert.equal(nextDescRef.value, null);
	});

	it('scrollPausedRef.value starts as false', () => {
		assert.equal(scrollPausedRef.value, false);
	});

	it('refs are mutable (not frozen)', () => {
		const original = narrationSpeakingRef.value;
		narrationSpeakingRef.value = true;
		assert.equal(narrationSpeakingRef.value, true);
		narrationSpeakingRef.value = original;
	});
});
