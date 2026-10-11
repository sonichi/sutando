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

const { createSessionEndPipeline, createCallEndReporter, sessionEndTask, worthATask, SESSION_END_MAX_CHARS } = await import('../src/voice-session-end.js');
const { submitVoiceSessionEndTask } = await import('../src/task-bridge.js');

const item = (role: string, content: string, timestamp = 0) => ({ role, content, timestamp }) as never;
const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));
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

	it('voice-agent reports each call on hang-up, and a call in progress at shutdown through bodhi', () => {
		const src = readFileSync(join(import.meta.dirname, '..', 'src', 'voice-agent.ts'), 'utf-8');
		assert.match(src, /postSessionPipeline: callEnd\.pipeline,/);
		assert.match(src, /drainPostSession: true,/, 'close() is followed by process.exit');
		assert.match(src, /onClientConnected: \(\) => callEnd\.clientConnected\(\),/);
		assert.match(src, /onClientDisconnected: \(\) => \{\n\t\t\tcallEnd\.clientDisconnected\(\);/);
		assert.match(src, /subscribe\('turn\.end', \(\) => callEnd\.collect\(\)\);/);
		assert.match(src, /beforeConversationClear = \(\) => callEnd\.collect\(\);/);
		assert.match(src, /await submitVoiceSessionEndTask\(summary, transcript\);/);
	});
});

describe('a call ends when the user hangs up', () => {
	function call() {
		let t = 1000;
		const live: unknown[] = [];
		const ended: Array<{ reason: string; items: string[] }> = [];
		const r = createCallEndReporter({
			sessionId: 's1', items: () => live as never, graceMs: 30, now: () => t,
			onEnded: (s) => { ended.push({ reason: s.reason, items: s.items.map((i) => i.content) }); },
		});
		const say = (role: string, content: string) => { t += 10; live.push(item(role, content, t)); };
		return { r, live, ended, say, advance: (ms: number) => { t += ms; } };
	}

	it('no client back within the grace period: one task for the call', async () => {
		const c = call();
		c.r.clientConnected();
		c.say('user', 'check PR 5308');
		c.say('assistant', 'merged');
		c.r.clientDisconnected();
		await tick(60);
		assert.deepEqual(c.ended, [{ reason: 'user_hangup', items: ['check PR 5308', 'merged'] }]);
	});

	it('a refresh or a blip that reconnects within the grace period is the same call', async () => {
		const c = call();
		c.r.clientConnected();
		c.say('user', 'first');
		c.r.clientDisconnected();
		await tick(10);
		c.r.clientConnected();
		c.say('user', 'second');
		c.r.clientDisconnected();
		await tick(60);
		assert.deepEqual(c.ended, [{ reason: 'user_hangup', items: ['first', 'second'] }]);
	});

	it('a goodbye that clears the live conversation before the hang-up still reports what was said', async () => {
		const c = call();
		c.r.clientConnected();
		c.say('user', 'remind me Friday');
		c.say('assistant', 'done');
		c.say('user', 'goodbye');
		c.r.collect(); // right before the clear
		c.live.length = 0;
		c.r.clientDisconnected();
		await tick(60);
		assert.deepEqual(c.ended[0].items, ['remind me Friday', 'done', 'goodbye']);
	});

	it('the next call reports only its own lines; earlier lines still in the live context are not repeated', async () => {
		const c = call();
		c.r.clientConnected();
		c.say('user', 'call one');
		c.r.clientDisconnected();
		await tick(60);
		c.advance(100);
		c.r.clientConnected();
		c.say('user', 'call two');
		c.r.clientDisconnected();
		await tick(60);
		assert.deepEqual(c.ended.map((e) => e.items), [['call one'], ['call two']]);
	});

	it('at shutdown a call in progress is reported once through bodhi\'s pipeline; an ended call is not reported again', async () => {
		const c = call();
		c.r.clientConnected();
		c.say('user', 'in progress');
		const snapItems = [...c.live];
		await c.r.pipeline.dispatch(input(snapItems, 'user_hangup')).report;
		assert.deepEqual(c.ended.map((e) => e.items), [['in progress']]);
		await c.r.pipeline.dispatch(input(snapItems, 'user_hangup')).report;
		assert.equal(c.ended.length, 1, 'no call left to report');
	});

	it('a call the owner never spoke in sends nothing', async () => {
		const c = call();
		c.r.clientConnected();
		c.say('assistant', 'Hi!');
		c.r.clientDisconnected();
		await tick(60);
		assert.deepEqual(c.ended, []);
	});
});
