// The voice conversation log's non-blocking writers: conversation.log through fs/promises, the
// sqlite row through the conversation store's worker thread.
// Run: npx tsx --test --test-force-exit tests/conversation-log-async-writers.test.ts
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

const TMP = mkdtempSync(join(tmpdir(), 'convlog-async-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
process.env.SUTANDO_CONVERSATION_DB = join(TMP, 'data', 'conversation.sqlite');
for (const d of ['logs', 'data', 'state']) mkdirSync(join(TMP, d), { recursive: true });
after(() => rmSync(TMP, { recursive: true, force: true }));

const { logConversationAsync, logSessionBoundaryAsync } = await import('../src/task-bridge.js');
const { recordConversation } = await import('../src/conversation-store.js');

const rows = () => {
	const db = new DatabaseSync(process.env.SUTANDO_CONVERSATION_DB!);
	try { return db.prepare('SELECT kind, text, session_id FROM voice ORDER BY id').all() as Array<{ kind: string; text: string; session_id: string | null }>; }
	finally { db.close(); }
};

describe('non-blocking conversation log writers', () => {
	it('a line and a boundary reach conversation.log and sqlite, in order, with the given time', async () => {
		const at = new Date(Date.UTC(2026, 9, 10, 18, 0, 5));
		const pending = logConversationAsync('user', 'check PR\n5308', 's1', at);
		assert.ok(pending instanceof Promise, 'returns at once');
		await pending;
		await logConversationAsync('assistant', 'merged', 's1');
		await logSessionBoundaryAsync('voice_goodbye');
		const log = readFileSync(join(TMP, 'logs', 'conversation.log'), 'utf-8').trim().split('\n');
		assert.equal(log[0], '2026-10-10T18:00:05.000Z|user|check PR 5308');
		assert.match(log[1], /\|assistant\|merged$/);
		assert.match(log[2], /\|SESSION_END\|voice_goodbye$/);
		const r = rows();
		assert.deepEqual(r.map((x) => x.text), ['check PR 5308', 'merged', 'voice_goodbye']);
		assert.equal(r[0].session_id, 's1');
	});

	it('the worker writes the same row the synchronous writer does', async () => {
		recordConversation('user', 'sync row', 's2');
		await logConversationAsync('user', 'async row', 's2');
		const r = rows().filter((x) => x.session_id === 's2');
		assert.deepEqual(r.map((x) => [x.kind, x.text]), [[r[0].kind, 'sync row'], [r[0].kind, 'async row']]);
	});
});
