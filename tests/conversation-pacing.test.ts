// A background result enters the conversation only at a pause.
// Run: npx tsx --test tests/conversation-pacing.test.ts
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { createConversationPacer } from '../src/conversation-pacing.js';

function clock() {
	let t = 1_000_000;
	return { now: () => t, advance: (ms: number) => { t += ms; } };
}

describe('conversation pacer', () => {
	it('is quiet only after the set silence, never while either side speaks', () => {
		const c = clock();
		const p = createConversationPacer({ quietMs: 5_000, now: c.now });
		assert.equal(p.isQuiet(), true, 'nothing has happened yet');
		p.onUserSpeechStarted();
		c.advance(10_000);
		assert.equal(p.isQuiet(), false, 'the user is still speaking');
		p.onUserSpeechEnded();
		p.onTurnStart();
		c.advance(10_000);
		assert.equal(p.isQuiet(), false, 'the model is speaking');
		p.onTurnEnd();
		c.advance(4_000);
		assert.equal(p.isQuiet(), false);
		c.advance(1_000);
		assert.equal(p.isQuiet(), true);
	});

	it('waits longer after an interrupted answer, until a turn completes', () => {
		const c = clock();
		const p = createConversationPacer({ quietMs: 5_000, afterInterruptMs: 15_000, now: c.now });
		p.onTurnStart();
		p.onTurnInterrupted();
		c.advance(6_000);
		assert.equal(p.isQuiet(), false, 'the interrupted question may still be open');
		c.advance(9_000);
		assert.equal(p.isQuiet(), true);
		p.onTurnStart();
		p.onTurnInterrupted();
		p.onTurnStart();
		p.onTurnEnd();
		c.advance(5_000);
		assert.equal(p.isQuiet(), true, 'a completed answer clears the interruption');
	});

	it('gives up waiting after maxWaitMs', async () => {
		const p = createConversationPacer({ maxWaitMs: 50, pollMs: 10 });
		p.onUserSpeechStarted();
		const started = Date.now();
		await p.waitForQuiet();
		assert.ok(Date.now() - started >= 50);
	});
});
