// After a voice session closes, the core gets a task about it (bodhi's post-session pipeline contract).
// Run: npx tsx --test --test-force-exit tests/voice-session-end.test.ts
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'session-end-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
for (const d of ['tasks', 'results', 'state']) mkdirSync(join(TMP, d), { recursive: true });
after(() => rmSync(TMP, { recursive: true, force: true }));

const { createSessionEndPipeline, sessionEndTask, worthATask, SESSION_END_MAX_CHARS } = await import('../src/voice-session-end.js');
const { submitVoiceSessionEndTask } = await import('../src/task-bridge.js');

const item = (role: string, content: string) => ({ role, content, timestamp: 0 }) as never;
function input(items: unknown[], reason = 'client_disconnect') {
	return {
		sessionId: 's1', reason,
		build: () => ({
			snapshot: {
				sessionId: 's1', userId: 'user', initialAgentName: 'main', finalAgentName: 'main', transferPath: ['main'], reason,
				startedAt: Date.UTC(2026, 9, 10, 18, 0), endedAt: Date.UTC(2026, 9, 10, 18, 12), durationMs: 12 * 60_000,
				conversation: { items }, metrics: { turnCount: 4, toolCallCount: 1, agentTransferCount: 0 },
			},
			stores: {},
		}),
	} as never;
}

describe('session-end pipeline', () => {
	it('a closed session reaches the step once, with its facts and its items', async () => {
		const got: Array<{ reason: string; turnCount: number; items: unknown[] }> = [];
		const p = createSessionEndPipeline((s) => { got.push({ reason: s.reason, turnCount: s.turnCount, items: [...s.items] }); });
		const processed: string[] = [];
		p.events.onProcessed((r) => processed.push((r.results[0] as { status: string }).status));
		const run = p.dispatch(input([item('user', 'check PR 5308'), item('assistant', 'merged')]));
		assert.equal(run.outcome, 'accepted');
		const report = await run.report;
		assert.equal(got.length, 1);
		assert.equal(got[0].reason, 'client_disconnect');
		assert.equal(got[0].turnCount, 4);
		assert.equal(got[0].items.length, 2);
		assert.equal((report.results[0] as { status: string }).status, 'completed');
		assert.deepEqual(processed, ['completed']);
		assert.equal(p.stats().completed, 1);
	});

	it('a session the owner never spoke in is skipped', async () => {
		let calls = 0;
		const p = createSessionEndPipeline(() => { calls++; });
		const report = await p.dispatch(input([item('assistant', 'Hi, how can I help?'), item('user', '[System: greeting]')])).report;
		assert.equal(calls, 0);
		assert.equal((report.results[0] as { status: string }).status, 'skipped');
	});

	it('a failing step resolves its report as failed and never throws out of dispatch', async () => {
		const logs: string[] = [];
		const p = createSessionEndPipeline(() => { throw new Error('disk full'); }, (m) => logs.push(m));
		const report = await p.dispatch(input([item('user', 'hello')])).report;
		assert.equal((report.results[0] as { status: string }).status, 'failed');
		assert.ok(logs.some((l) => l.includes('disk full')));
		await p.drain();
	});
});

describe('the session-end task', () => {
	it('says when the session ran and how it ended, asks for no reply, and carries its spoken lines', async () => {
		const s = { sessionId: 's1', reason: 'client_disconnect', startedAt: Date.UTC(2026, 9, 10, 18, 0), endedAt: Date.UTC(2026, 9, 10, 18, 12),
			durationMs: 12 * 60_000, turnCount: 4, toolCallCount: 1,
			items: [item('user', 'check PR 5308'), item('tool_call', '{}'), item('assistant', 'It is merged.')] };
		assert.ok(worthATask(s));
		const { summary, transcript } = sessionEndTask(s);
		assert.match(summary, /^VOICE_SESSION_ENDED: the voice session s1 ended \(client_disconnect\)\. 2026-10-10T18:00:00\.000Z to 2026-10-10T18:12:00\.000Z, about 12 min, 4 turns, 1 tool calls\./);
		assert.match(summary, /\[no-send\]/);
		assert.equal(transcript, 'user: check PR 5308\nassistant: It is merged.');
		const id = await submitVoiceSessionEndTask(summary, transcript);
		const body = readFileSync(join(TMP, 'tasks', `${id}.txt`), 'utf-8');
		assert.match(body, /^source: voice$/m);
		assert.match(body, /^priority: low$/m);
		assert.match(body, /^task: VOICE_SESSION_ENDED:/m);
		assert.match(body, /--- the session's spoken lines[^\n]*---\n[\s\S]*user: check PR 5308/);
		assert.equal(readdirSync(join(TMP, 'tasks')).filter((f) => f.endsWith('.txt')).length, 1);
	});

	it('a long session keeps its end', () => {
		const long = Array.from({ length: 400 }, (_, i) => item(i % 2 ? 'assistant' : 'user', `line ${i} ${'x'.repeat(80)}`));
		const { transcript } = sessionEndTask({ sessionId: 's', reason: 'r', startedAt: 0, endedAt: 1, durationMs: 1, turnCount: 1, toolCallCount: 0, items: long });
		assert.ok(transcript.length <= SESSION_END_MAX_CHARS + 60);
		assert.match(transcript, /^\[… \d+ earlier characters\]/);
		assert.match(transcript, /line 399 x+$/);
	});

	it('voice-agent passes the pipeline to bodhi and writes the task from it', () => {
		const src = readFileSync(join(import.meta.dirname, '..', 'src', 'voice-agent.ts'), 'utf-8');
		assert.match(src, /postSessionPipeline: createSessionEndPipeline\(async \(ended\) => \{\n\t\t\tconst \{ summary, transcript \} = sessionEndTask\(ended\);\n\t\t\tawait submitVoiceSessionEndTask\(summary, transcript\);/);
		assert.match(src, /drainPostSession: true,/, 'close() is followed by process.exit');
	});
});
