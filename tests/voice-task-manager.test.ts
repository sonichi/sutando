// The voice task manager: every result goes through one queue (one hand-over at a time, at a
// pause, confirmed by the model's turn), and a durable record of how each result reached the user.
// Run: npx tsx --test --test-force-exit tests/voice-task-manager.test.ts
import { describe, it, after, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-task-manager-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
const MONTH = new Date().toISOString().slice(0, 7);
for (const d of ['tasks', 'results', join('tasks', 'archive', MONTH), join('results', 'archive', MONTH), join('state', 'activity')]) mkdirSync(join(TMP, d), { recursive: true });

const { createResultQueue, createVoiceTaskStore, frameBatch, shouldReplayDeduped } = await import('../src/voice-task-manager.js');
const { frameTaskResult } = await import('../src/inject-framing.js');
const tb = await import('../src/task-bridge.js');
const { _pendingTasksForTest, startResultWatcher, voiceTaskStore, REPEATED_REQUEST_NOTE, _forwardOfflineThenArchive } = tb;

after(() => rmSync(TMP, { recursive: true, force: true }));

const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** A queue with no real waiting: every pause is immediate, the session state is a flag. */
function harness(opts: { ready?: boolean; store?: ReturnType<typeof createVoiceTaskStore> } = {}) {
	const injected: string[] = [];
	const fellBack: string[][] = [];
	const state = { ready: opts.ready ?? true };
	const q = createResultQueue({
		canInject: () => state.ready,
		inject: (t) => injected.push(t),
		waitForQuiet: async () => {},
		fallback: (items) => fellBack.push(items.map((i) => i.text)),
		store: opts.store,
		sleep: async () => {},
		turnTimeoutMs: 1_000,
	});
	return { q, injected, fellBack, state };
}

describe('result queue', () => {
	it('two results arriving together are handed over once, and both are in it', async () => {
		const { q, injected } = harness();
		q.enqueue({ text: '#5140 is merged.', taskId: 'task-5140' });
		q.enqueue({ text: '#5167 is blocked on CI.', taskId: 'task-5167' });
		await tick(10);
		assert.equal(injected.length, 1);
		assert.match(injected[0], /2 task results arrived together/);
		assert.match(injected[0], /#5140 is merged/);
		assert.match(injected[0], /#5167 is blocked on CI/);
	});

	it('a result arriving while the model answers the previous one waits for that turn to end', async () => {
		const { q, injected } = harness();
		q.enqueue({ text: 'first' });
		await tick(10);
		q.enqueue({ text: 'second' });
		await tick(10);
		assert.equal(injected.length, 1, 'held while the first is being spoken');
		q.onTurnEnd();
		await tick(10);
		assert.equal(injected.length, 2);
		assert.match(injected[1], /second/);
	});

	it('an answer the user cut off is handed over once more, then recorded as not confirmed', async () => {
		const store = createVoiceTaskStore(join(TMP, 'state', 'cut.json'));
		const { q, injected } = harness({ store });
		q.enqueue({ text: 'the weather', taskId: 'task-w' });
		await tick(10);
		q.onTurnInterrupted();
		await tick(10);
		assert.equal(injected.length, 2);
		q.onTurnInterrupted();
		await tick(10);
		assert.equal(injected.length, 2, 'no third try');
		assert.equal(store.get('task-w')?.delivery, 'injected');
	});

	it('a finished turn after the hand-over records the result as spoken', async () => {
		const store = createVoiceTaskStore(join(TMP, 'state', 'spoken.json'));
		const { q } = harness({ store });
		q.enqueue({ text: 'done', taskId: 'task-s' });
		await tick(10);
		q.onTurnEnd();
		await tick(10);
		assert.equal(store.get('task-s')?.delivery, 'spoken');
	});

	it('a session that cannot take results sends the batch to the fallback and records dm', async () => {
		const store = createVoiceTaskStore(join(TMP, 'state', 'dm.json'));
		const { q, injected, fellBack } = harness({ ready: false, store });
		q.enqueue({ text: 'the car picture', taskId: 'task-car' });
		await tick(10);
		assert.deepEqual(injected, []);
		assert.deepEqual(fellBack, [['the car picture']]);
		assert.equal(store.get('task-car')?.delivery, 'dm');
	});

	it('a held queue (meeting) neither injects nor falls back, and sends everything once it ends', async () => {
		const injected: string[] = [];
		const fellBack: string[][] = [];
		const meeting = { on: true };
		const q = createResultQueue({
			held: () => meeting.on,
			canInject: () => true,
			inject: (t) => injected.push(t),
			waitForQuiet: async () => {},
			fallback: (items) => fellBack.push(items.map((i) => i.text)),
			sleep: () => new Promise((r) => setTimeout(r, 1)),
			heldPollMs: 5,
		});
		q.enqueue({ text: 'build done' });
		q.enqueue({ text: '[System: call done]', framed: true });
		await tick(50);
		assert.deepEqual(injected, []);
		assert.deepEqual(fellBack, [], 'a meeting is not a reason to send it to the DM');
		meeting.on = false;
		await tick(50);
		assert.equal(injected.length, 1);
		assert.match(injected[0], /2 task results arrived together/);
		assert.ok(injected[0].includes('[System: call done]') && !injected[0].includes('TASK_RESULT_START>\n[System: call done]'), 'a framed item is not wrapped as a task result');
	});

	it('one result is framed exactly as before', () => {
		assert.equal(frameBatch([{ text: 'x' }]), frameTaskResult('x'));
	});
});

describe('voice task store', () => {
	it('survives a restart: a new store on the same file reads what the old one wrote', () => {
		const path = join(TMP, 'state', 'restart.json');
		createVoiceTaskStore(path).set('task-a', 'spoken');
		assert.equal(createVoiceTaskStore(path).get('task-a')?.delivery, 'spoken');
	});

	it('never downgrades a spoken result', () => {
		const s = createVoiceTaskStore(join(TMP, 'state', 'down.json'));
		s.set('task-b', 'spoken');
		s.set('task-b', 'dm');
		assert.equal(s.get('task-b')?.delivery, 'spoken');
	});

	it('keeps at most 200 tasks, newest first, and a foreign file reads as empty', () => {
		const path = join(TMP, 'state', 'cap.json');
		let t = 0;
		const s = createVoiceTaskStore(path, () => ++t);
		for (let i = 0; i < 205; i++) s.set(`task-${i}`, 'spoken');
		const tasks = JSON.parse(readFileSync(path, 'utf-8')).tasks as Record<string, unknown>;
		assert.equal(Object.keys(tasks).length, 200);
		assert.ok(!('task-0' in tasks) && 'task-204' in tasks);
		writeFileSync(path, '{"tasks": "nope"}');
		assert.equal(s.get('task-204'), undefined);
	});

	it('replays a repeat only when the user never heard the first result', () => {
		assert.equal(shouldReplayDeduped({ delivery: 'dm', at: 1 }), true);
		assert.equal(shouldReplayDeduped(undefined), true);
		assert.equal(shouldReplayDeduped({ delivery: 'spoken', at: 1 }), false);
		assert.equal(shouldReplayDeduped({ delivery: 'injected', at: 1 }), false);
	});
});

describe('result watcher: repeated requests', () => {
	const heard: Array<{ text: string; note?: string; taskId?: string }> = [];
	startResultWatcher((text, note, meta) => heard.push({ text, note, taskId: meta?.taskId }), () => true);
	let seq = 0;
	const voiceTask = (where: string, id: string, text: string) =>
		writeFileSync(join(TMP, where, `${id}.txt`), `id: ${id}\nsource: voice\nchannel_id: local-voice\ntask: ${text}\n`);
	/** An earlier request whose result is already archived. */
	function earlier(text: string, result: string): string {
		const id = `task-${1_800_000_100_000 + ++seq}`;
		voiceTask(join('tasks', 'archive', MONTH), id, text);
		writeFileSync(join(TMP, 'results', 'archive', MONTH, `${id}.txt`), result);
		return id;
	}
	/** The repeat: a new voice task the core answers with a dedup pointer. */
	function repeat(text: string, heldBy: string): string {
		const id = `task-${1_800_000_200_000 + ++seq}`;
		voiceTask('tasks', id, text);
		_pendingTasksForTest.set(id, { submittedAt: Date.now(), timeoutMs: 3_600_000, dmOnTimeout: false, taskText: text });
		writeFileSync(join(TMP, 'results', `${id}.txt`), `[deduped: ${heldBy}]`);
		return id;
	}
	beforeEach(() => { heard.length = 0; _pendingTasksForTest.clear(); });

	it('a result that went to the DM while voice was offline is spoken when the request is repeated', async () => {
		const first = earlier('check PR 3509', 'PR 3509 has two approvals and green CI.');
		const forwarded = await _forwardOfflineThenArchive(first, `${first}.txt`, 'PR 3509 has two approvals and green CI.', false, async () => {}, 0);
		assert.equal(forwarded, true);
		assert.equal(voiceTaskStore.get(first)?.delivery, 'dm');
		repeat('check PR 3509 again', first);
		await tick(2_500);
		assert.deepEqual(heard, [{ text: 'PR 3509 has two approvals and green CI.', note: REPEATED_REQUEST_NOTE, taskId: first }]);
	});

	it('a result the user already heard stays silent on a repeat, also after a restart', async () => {
		const first = earlier('check PR 4000', 'PR 4000 is merged.');
		createVoiceTaskStore(join(TMP, 'state', 'voice-tasks.json')).set(first, 'spoken');
		repeat('check PR 4000 again', first);
		await tick(2_500);
		assert.deepEqual(heard, []);
	});

	it('a result still on its way is not spoken twice', async () => {
		const id = `task-${1_800_000_300_000 + ++seq}`;
		voiceTask('tasks', id, 'check PR 4100');
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'PR 4100 is open.');
		repeat('check PR 4100 again', id);
		await tick(2_500);
		assert.ok(heard.every((h) => h.note !== REPEATED_REQUEST_NOTE), JSON.stringify(heard));
	});
});
