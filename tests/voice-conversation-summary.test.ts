// The voice conversation's compressed summary (bodhi's ConversationContext slot, filled by sutando)
// and the earlier conversation a work task carries.
// Run: npx tsx --test --test-force-exit tests/voice-conversation-summary.test.ts
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'voice-summary-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
after(() => rmSync(TMP, { recursive: true, force: true }));

const { ConversationContext } = await import('bodhi-realtime-agent');
const { createConversationSummarizer, renderItems } = await import('../src/voice-conversation-summary.js');
const tb = await import('../src/task-bridge.js');

function conversation(turns: number) {
	const ctx = new ConversationContext();
	for (let i = 1; i <= turns; i++) { ctx.addUserMessage(`question ${i}`); ctx.addAssistantMessage(`answer ${i}`); }
	return ctx;
}

describe('the compressed summary', () => {
	it('below the bound nothing is summarized', async () => {
		const ctx = conversation(3);
		let calls = 0;
		const s = createConversationSummarizer({ context: () => ctx, summarize: async () => { calls++; return 'x'; }, thresholdTokens: 10_000 });
		await s.onTurnEnd();
		assert.equal(calls, 0);
		assert.equal(ctx.summary, null);
	});

	it('past the bound the older turns become the summary and the last ones stay verbatim', async () => {
		const ctx = conversation(10);
		ctx.markCheckpoint(); // bodhi's history writer has persisted everything
		let seen: string[] = [];
		const evicted: number[] = [];
		const s = createConversationSummarizer({
			context: () => ctx, thresholdTokens: 1, keepRecent: 4,
			summarize: async (_prev, items) => { seen = items.map((i) => i.content); return 'The user asked 8 questions.'; },
			onEvicted: (n) => evicted.push(n),
		});
		await s.onTurnEnd();
		assert.equal(seen.length, 16, 'everything but the last 4 items');
		assert.equal(ctx.items.length, 0, 'bodhi drops every persisted item');
		assert.deepEqual(evicted, [20]);
		assert.match(ctx.summary ?? '', /^The user asked 8 questions\./);
		assert.match(ctx.summary ?? '', /Then, verbatim:\nuser: question 9\nassistant: answer 9\nuser: question 10\nassistant: answer 10$/,
			'the persisted items the model did not summarize are kept verbatim, not lost');
	});

	it('turns added while the model works and not yet persisted stay in the live context', async () => {
		const ctx = conversation(6);
		ctx.markCheckpoint();
		let release!: (s: string) => void;
		const s = createConversationSummarizer({ context: () => ctx, thresholdTokens: 1, keepRecent: 2, summarize: () => new Promise((r) => { release = r; }) });
		const run = s.onTurnEnd();
		ctx.addUserMessage('new question');
		release('summary');
		await run;
		assert.deepEqual(ctx.items.map((i) => i.content), ['new question']);
	});

	it('a reset while the model works discards its summary; clear() empties the slot', async () => {
		const ctx = conversation(6);
		ctx.markCheckpoint();
		let release!: (s: string) => void;
		const s = createConversationSummarizer({ context: () => ctx, thresholdTokens: 1, keepRecent: 2, summarize: () => new Promise((r) => { release = r; }) });
		const run = s.onTurnEnd();
		ctx.clear();
		s.clear();
		release('stale summary');
		await run;
		assert.ok(!ctx.summary, 'a goodbye\'s conversation does not reach the next session');
		ctx.setSummary('old');
		s.clear();
		assert.ok(!ctx.summary);
	});

	it('a failed summary changes nothing', async () => {
		const ctx = conversation(6);
		ctx.markCheckpoint();
		const s = createConversationSummarizer({ context: () => ctx, thresholdTokens: 1, keepRecent: 2, summarize: async () => { throw new Error('HTTP 503'); } });
		await s.onTurnEnd();
		assert.equal(ctx.items.length, 12);
		assert.equal(ctx.summary, null);
	});

	it('renders only spoken lines', () => {
		assert.equal(renderItems([{ role: 'user', content: 'hi\nthere' }, { role: 'tool_call', content: '{}' }, { role: 'assistant', content: 'hello' }]), 'user: hi there\nassistant: hello');
	});
});

describe('the earlier conversation a work task carries', () => {
	it('the summary, then the last turns of the live session', () => {
		const items = Array.from({ length: 12 }, (_, i) => ({ role: i % 2 ? 'assistant' : 'user', content: `line ${i + 1}` }));
		tb.setVoiceTurnsProvider(() => ({ items: [...items, { role: 'tool_call', content: '{}' }], summary: 'Earlier: the user planned a trip.' }));
		try {
			const ctx = tb._voiceContextAtAsk() ?? '';
			assert.match(ctx, /^summary of the conversation before these turns:\nEarlier: the user planned a trip\.\n\n/);
			assert.ok(!ctx.includes('line 2\n'), 'only the last turns');
			assert.equal(ctx.split('\n\n')[1].split('\n').length, tb.CONTEXT_RECENT_ITEMS);
			assert.match(ctx, /assistant: line 12$/);
		} finally {
			tb.setVoiceTurnsProvider(null);
		}
	});

	it('without a live session the task falls back to the log', () => {
		tb.setVoiceTurnsProvider(null);
		assert.equal(tb._voiceContextAtAsk(), null);
		const src = readFileSync(join(import.meta.dirname, '..', 'src', 'task-bridge.ts'), 'utf-8');
		assert.match(src, /recentAtAsk = _voiceContextAtAsk\(\) \?\? getRecentConversation\(4\);/);
	});

	it('voice-agent fills the summary, clears it with the conversation, and hands it to work tasks', () => {
		const src = readFileSync(join(import.meta.dirname, '..', 'src', 'voice-agent.ts'), 'utf-8');
		assert.match(src, /summary: session\.conversationContext\.summary,/);
		assert.match(src, /resetConversationContext\(reason\)\.cleared \?\? 0;\n\t\tconversationSummary\.clear\(\);/);
		assert.match(src, /subscribe\('turn\.end', \(\) => \{ if \(!sessionEnding\) void conversationSummary\.onTurnEnd\(\); \}\);/);
	});
});
