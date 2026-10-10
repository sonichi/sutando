// The voice conversation log as a bodhi ConversationHistoryStore: bodhi's history writer hands it
// each turn's items, and it orders the session-end boundary against them by time.
// Run: npx tsx --test --test-force-exit tests/conversation-log-store.test.ts
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { ConversationContext, ConversationHistoryWriter, EventBus } from 'bodhi-realtime-agent';
import { ConversationLogStore } from '../src/conversation-log-store.js';

const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));

function harness(opts: { suppress?: () => boolean; waitMs?: number } = {}) {
	const lines: string[] = [];
	const store = new ConversationLogStore({
		log: (role, text) => lines.push(`${role}|${text}`),
		boundary: (reason) => lines.push(`SESSION_END|${reason}`),
		suppress: opts.suppress ?? (() => false),
	}, opts.waitMs ?? 50);
	const bus = new EventBus();
	const ctx = new ConversationContext();
	new ConversationHistoryWriter('s1', 'user', 'main', bus, ctx, store);
	const turnEnd = () => bus.publish('turn.end', { sessionId: 's1', turnId: 't' } as never);
	return { lines, store, bus, ctx, turnEnd };
}

describe('conversation log through bodhi\'s history writer', () => {
	it('each turn\'s spoken lines are written after turn.end, in order, once', async () => {
		const h = harness();
		h.ctx.addUserMessage('hello');
		h.ctx.addToolCall({ toolCallId: 'c1', toolName: 'work', args: {} });
		h.ctx.addAssistantMessage('hi there');
		assert.deepEqual(h.lines, [], 'nothing is written synchronously');
		h.turnEnd();
		await h.store.drain();
		await tick(5);
		assert.deepEqual(h.lines, ['user|hello', 'assistant|hi there'], 'tool items are not spoken lines');
		h.turnEnd();
		await tick(5);
		assert.equal(h.lines.length, 2, 'a later turn.end does not write them again');
	});

	it('a goodbye said before the boundary is written before it; a line after it, after', async () => {
		const h = harness();
		h.ctx.addUserMessage('goodbye');
		h.ctx.addAssistantMessage('bye');
		h.store.markBoundary('voice_goodbye');
		await tick(2);
		h.ctx.addAssistantMessage('talk to you next time');
		h.turnEnd();
		await tick(5);
		assert.deepEqual(h.lines, ['user|goodbye', 'assistant|bye', 'SESSION_END|voice_goodbye', 'assistant|talk to you next time']);
	});

	it('a boundary with nothing after it is written once its wait is over', async () => {
		const h = harness({ waitMs: 20 });
		h.ctx.addUserMessage('bye');
		h.turnEnd();
		await tick(5);
		h.store.markBoundary('user_goodbye');
		assert.deepEqual(h.lines, ['user|bye']);
		await tick(40);
		assert.deepEqual(h.lines, ['user|bye', 'SESSION_END|user_goodbye']);
		await tick(40);
		assert.equal(h.lines.length, 2, 'written once');
	});

	it('while a goodbye closes the session its remaining lines are not written', async () => {
		let ending = false;
		const h = harness({ suppress: () => ending });
		h.ctx.addUserMessage('please summarize');
		h.turnEnd();
		await tick(5);
		ending = true;
		h.ctx.addAssistantMessage('Farewell. Talk to you next time.');
		h.turnEnd();
		await tick(5);
		assert.deepEqual(h.lines, ['user|please summarize']);
	});

	it('voice-agent hands bodhi the store and logs no turn itself', () => {
		const src = readFileSync(join(import.meta.dirname, '..', 'src', 'voice-agent.ts'), 'utf-8');
		assert.match(src, /conversationHistoryStores: \[conversationLogStore\],/);
		assert.doesNotMatch(src, /logConversation\(item\.role/);
		assert.doesNotMatch(src, /logSessionBoundary\('(user|voice)_goodbye'\)/, 'the boundary is ordered by the store');
		assert.match(src, /suppress: \(\) => sessionEnding,/);
	});
});
