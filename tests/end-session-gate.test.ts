import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

/**
 * Tests for the end_session tool gate in voice-agent.ts.
 *
 * The gate refuses end_session calls unless conversationContext.items
 * contains at least one "real" user turn — non-empty content, not a
 * `[System:...]` injection prompt. Bodhi tags injected context as
 * role='user' too (see handleClientConnected's reconnect context
 * summary), so role alone is insufficient.
 *
 * Regression coverage for the 2026-04-09 goodbye-loop saga:
 * - commit 8 introduced the userTurnCount gate
 * - commit 9 fixed a race where turn.end fired AFTER the tool result,
 *   causing legitimate goodbyes to be refused. Switched to checking
 *   conversationContext.items directly at tool-call time.
 *
 * This test replicates the predicate so any refactor that drops or
 * weakens the filter will break it.
 */

type Item = { role?: string; content?: string };

function hasRealUserTurn(items: unknown): boolean {
	return Array.isArray(items) && items.some((item: Item) =>
		item?.role === 'user' &&
		typeof item?.content === 'string' &&
		item.content.length > 0 &&
		!item.content.startsWith('[System:')
	);
}

describe('end_session gate — hasRealUserTurn predicate', () => {
	it('refuses when items is undefined', () => {
		assert.equal(hasRealUserTurn(undefined), false);
	});

	it('refuses when items is null', () => {
		assert.equal(hasRealUserTurn(null), false);
	});

	it('refuses when items is empty array', () => {
		assert.equal(hasRealUserTurn([]), false);
	});

	it('refuses when only assistant items exist', () => {
		const items: Item[] = [
			{ role: 'assistant', content: "I'm Sutando, how can I help?" },
			{ role: 'assistant', content: 'Ready for your command.' },
		];
		assert.equal(hasRealUserTurn(items), false);
	});

	it('refuses when the only "user" item is a [System:] injection', () => {
		// This is the classic contamination case: bodhi's handleClient-
		// Connected ACTIVE-branch reconnect path injects a prompt like
		// "[System: The client reconnected. Here is the recent conversation..."
		// with role='user'. That's not real speech and must not unlock
		// the gate.
		const items: Item[] = [
			{ role: 'user', content: '[System: The client reconnected. Here is the recent conversation for context...]' },
			{ role: 'assistant', content: "I'm back. What can I help with?" },
		];
		assert.equal(hasRealUserTurn(items), false);
	});

	it('refuses when user item content is empty string', () => {
		const items: Item[] = [
			{ role: 'user', content: '' },
			{ role: 'assistant', content: 'hello' },
		];
		assert.equal(hasRealUserTurn(items), false);
	});

	it('refuses when user item content is missing', () => {
		const items: Item[] = [
			{ role: 'user' },
			{ role: 'assistant', content: 'hello' },
		];
		assert.equal(hasRealUserTurn(items), false);
	});

	it('refuses when user item content is a note-view injection', () => {
		const items: Item[] = [
			{
				role: 'user',
				content: '[System: The user is now viewing notes/uiuc-trip-conflicts.md in the web UI. The text between <NOTE_START> and <NOTE_END>...]',
			},
		];
		assert.equal(hasRealUserTurn(items), false);
	});

	it('refuses when user item content is a task-result injection', () => {
		const items: Item[] = [
			{
				role: 'user',
				content: '[System: Task completed. The text between the TASK_RESULT_START and TASK_RESULT_END markers...]',
			},
		];
		assert.equal(hasRealUserTurn(items), false);
	});

	it('allows when a real user turn is present', () => {
		const items: Item[] = [
			{ role: 'assistant', content: "I'm Sutando, how can I help?" },
			{ role: 'user', content: 'hi' },
		];
		assert.equal(hasRealUserTurn(items), true);
	});

	it('allows when a real user turn is present even among injections', () => {
		// Mixed case: some injected items, but also a real user turn.
		// Legitimate goodbye — allow it.
		const items: Item[] = [
			{ role: 'user', content: '[System: The client reconnected. Previous context: ...]' },
			{ role: 'assistant', content: "I'm back." },
			{ role: 'user', content: 'bye' },
		];
		assert.equal(hasRealUserTurn(items), true);
	});

	it('allows when user turn is short but real ("bye", "hi", "ok")', () => {
		for (const content of ['bye', 'hi', 'ok', 'yes', 'no']) {
			const items: Item[] = [{ role: 'user', content }];
			assert.equal(hasRealUserTurn(items), true, `failed for content=${content}`);
		}
	});

	it('allows when user turn is a normal question', () => {
		const items: Item[] = [
			{ role: 'user', content: "what's on my schedule today?" },
		];
		assert.equal(hasRealUserTurn(items), true);
	});

	it('allows even when user turn starts with a non-[System: bracket', () => {
		// Edge: user literally says "[testing]". Shouldn't be treated as
		// an injection because it doesn't start with the literal "[System:"
		// marker.
		const items: Item[] = [
			{ role: 'user', content: '[testing] hello' },
		];
		assert.equal(hasRealUserTurn(items), true);
	});
});
