import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
	initialGoodbyeGuard,
	shouldFireGoodbye,
	createConversationClearHelper,
	clearStaleResumptionHandle,
} from '../src/voice-continuity.js';

describe('P7 D7.3 stale-repeat goodbye guard', () => {
	it('first detection fires and latches text + user-turn watermark', () => {
		const v = shouldFireGoodbye(initialGoodbyeGuard(), 'Goodbye!', 3);
		assert.equal(v.fire, true);
		assert.deepEqual(v.next, { lastText: 'Goodbye!', userTurnsAtFire: 3 });
	});

	it('the SAME farewell with no new user turn is suppressed (reconnect replay)', () => {
		const first = shouldFireGoodbye(initialGoodbyeGuard(), 'Goodbye!', 3);
		const replay = shouldFireGoodbye(first.next, 'Goodbye!', 3);
		assert.equal(replay.fire, false);
		assert.equal(replay.next, first.next, 'state unchanged on suppression');
	});

	it('the same farewell AFTER a new real user turn fires again', () => {
		const first = shouldFireGoodbye(initialGoodbyeGuard(), 'Goodbye!', 3);
		const again = shouldFireGoodbye(first.next, 'Goodbye!', 4);
		assert.equal(again.fire, true);
		assert.equal(again.next.userTurnsAtFire, 4);
	});

	it('a different farewell fires regardless of turn count', () => {
		const first = shouldFireGoodbye(initialGoodbyeGuard(), 'Goodbye!', 3);
		const other = shouldFireGoodbye(first.next, 'Bye for now!', 3);
		assert.equal(other.fire, true);
	});

	it('a rebased (fresh) guard fires at ANY count — the per-session reset contract', () => {
		// voice-agent's resetSessionGateState() replaces the guard whenever
		// userTurnCount resets; without the rebase, the next session's count
		// (restarted below the old watermark) would suppress a real goodbye.
		const first = shouldFireGoodbye(initialGoodbyeGuard(), 'Goodbye!', 3);
		assert.equal(first.fire, true);
		const nextSession = shouldFireGoodbye(initialGoodbyeGuard(), 'Goodbye!', 1);
		assert.equal(nextSession.fire, true);
	});
});

describe('P7 D7.3 centralized conversation clear (G-P7-8)', () => {
	it('empties items IN PLACE and rebases the cursor together', () => {
		const items: string[] = ['a', 'b', 'c'];
		const logs: string[] = [];
		const h = createConversationClearHelper(() => items, (m) => logs.push(m));
		h.cursor.index = 3;
		const cleared = h.clear('test');
		assert.equal(cleared, 3);
		assert.equal(items.length, 0, 'mutated in place (getter-backed array)');
		assert.equal(h.cursor.index, 0, 'cursor rebased WITH the clear');
		assert.ok(logs.some((l) => l.includes('cleared 3')));
	});

	it('rebases the cursor even when items are unreadable — a stale cursor against an emptied array is the bug', () => {
		const h = createConversationClearHelper(
			() => {
				throw new Error('no session yet');
			},
			() => {},
		);
		h.cursor.index = 7;
		assert.equal(h.clear('early'), 0);
		assert.equal(h.cursor.index, 0);
	});

	it('non-array items: clears nothing, still rebases', () => {
		const h = createConversationClearHelper(() => undefined, () => {});
		h.cursor.index = 2;
		assert.equal(h.clear('none'), 0);
		assert.equal(h.cursor.index, 0);
	});
});

describe('stale resumption-handle clearing (1008 \'Requested entity was not found\' staircase)', () => {
	it('clears BOTH handle copies before a fresh reconnect and reports it', () => {
		let smCleared = 0;
		const session = {
			transport: { config: { resumptionHandle: 'dead-handle-123' } },
			sessionManager: { clearResumptionHandle: () => { smCleared += 1; } },
		};
		assert.equal(clearStaleResumptionHandle(session), true);
		assert.equal(session.transport.config.resumptionHandle, undefined, 'transport copy cleared');
		assert.equal(smCleared, 1, 'sessionManager copy cleared');
	});

	it('no stale handle → false, still syncs the sessionManager copy', () => {
		let smCleared = 0;
		const session = {
			transport: { config: {} },
			sessionManager: { clearResumptionHandle: () => { smCleared += 1; } },
		};
		assert.equal(clearStaleResumptionHandle(session), false);
		assert.equal(smCleared, 1);
	});

	it('missing seams never throw (a broken shape must not break reconnect)', () => {
		assert.equal(clearStaleResumptionHandle(null), false);
		assert.equal(clearStaleResumptionHandle({}), false);
		assert.equal(clearStaleResumptionHandle({ transport: {} }), false);
	});
});
