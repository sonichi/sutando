// The relay agent: the work subagent, its result queue (one hand-over at a time, at a pause,
// confirmed by the model's turn), and the voice task table with the reconcile rule over it.
// Run: npx tsx --test --test-force-exit tests/relay-agent.test.ts
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

const { RelayAgent, relayAgentSubagentConfig, createResultQueue, createVoiceTaskStore, frameBatch, planReconcile, NOT_PICKED_MS, MAX_REPLAYS } = await import('../src/relay-agent.js');
const { frameTaskResult } = await import('../src/inject-framing.js');
const tb = await import('../src/task-bridge.js');
const { _pendingTasksForTest, voiceTaskStore, MISSED_RESULT_NOTE, _forwardOfflineThenArchive, reconcileVoiceTasks, voiceTaskRows, voiceTasksAhead } = tb;

after(() => rmSync(TMP, { recursive: true, force: true }));

const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** A queue with no real waiting: every pause is immediate, the session state is a flag. */
function harness(opts: { ready?: boolean; store?: ReturnType<typeof createVoiceTaskStore> } = {}) {
	const injected: string[] = [];
	const fellBack: string[][] = [];
	const state = { ready: opts.ready ?? true, takes: true };
	const q = createResultQueue({
		canInject: () => state.ready,
		inject: (t) => {
			if (!state.takes) return false;
			injected.push(t);
			return true;
		},
		fallback: (items) => fellBack.push(items.map((i) => i.text)),
		store: opts.store,
		sleep: async () => {},
		turnTimeoutMs: 1_000,
	});
	return { q, injected, fellBack, state };
}

describe('result queue', () => {
	it('two results arriving together go one per turn, each naming the request it answers', async () => {
		const { q, injected } = harness();
		q.enqueue({ text: '#5140 is merged.', taskId: 'task-5140', request: 'check PR 5140' });
		q.enqueue({ text: '#5167 is blocked on CI.', taskId: 'task-5167', request: 'check PR 5167' });
		await tick(10);
		assert.equal(injected.length, 1);
		assert.match(injected[0], /check PR 5140"[\s\S]*#5140 is merged/);
		q.onTurnEnd();
		await tick(10);
		assert.equal(injected.length, 2);
		assert.match(injected[1], /check PR 5167"[\s\S]*#5167 is blocked on CI/);
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

	it('each result in a batch names the request it answers, and only the batch ends the turn', () => {
		const out = frameBatch([{ text: '#5140 merged.', request: 'check PR 5140' }, { text: '#5167 blocked.', request: 'check PR 5167' }]);
		assert.match(out, /This answers the user's request: "check PR 5140"\.[\s\S]*#5140 merged\.[\s\S]*This answers the user's request: "check PR 5167"\.[\s\S]*#5167 blocked\./);
		assert.match(out, /Task result 1 of 2[\s\S]*Task result 2 of 2/);
		assert.equal(out.match(/wait for real input/g)?.length, 1, 'once, in the batch header');
		assert.match(frameBatch([{ text: 'x', request: 'draw a dog' }]), /This answers the user's request: "draw a dog"\.\]\n\n\[System: Task completed\./);
	});

	it('one result is framed exactly as before', () => {
		assert.equal(frameBatch([{ text: 'x' }]), frameTaskResult('x'));
	});
});

describe('result queue: a session that refuses the hand-over', () => {
	it('a refused batch is retried, then sent to the fallback and recorded dm', async () => {
		const store = createVoiceTaskStore(join(TMP, `refuse-${Math.random()}.json`));
		const { q, injected, fellBack, state } = harness({ store });
		state.takes = false;
		q.enqueue({ text: 'r1', taskId: 'task-r1' });
		for (let i = 0; i < 20 && fellBack.length === 0; i++) await tick(5);
		assert.deepEqual(injected, []);
		assert.deepEqual(fellBack, [['r1']]);
		assert.equal(store.get('task-r1')?.delivery, 'dm');
	});
});

describe('relay agent subagent', () => {
	it('runs as the persistent work subagent', async () => {
		const relay = new RelayAgent({ submit: async () => ({}), store: createVoiceTaskStore(join(TMP, 'sa.json')) });
		const config = relayAgentSubagentConfig(relay);
		assert.equal(config.lifetime, 'persistent_session');
		assert.equal(await config.persistentFactory!('relay-agent', config), relay);
	});

	it('a work call returns its submission status at once: the model says "working on it" as before', async () => {
		const status = { status: 'pending', taskId: 'task-1', queuedAhead: 1, message: 'Task has been queued. Got it, right after the one I\'m on.' };
		const relay = new RelayAgent({ submit: async () => status, store: createVoiceTaskStore(join(TMP, 'sb.json')) });
		assert.deepEqual(JSON.parse(await relay.invoke('Execute tool: work', { task: 'x' })), status);
	});

	it('results go through the relay agent\'s own queue, also ones that land before it is attached', async () => {
		const store = createVoiceTaskStore(join(TMP, `sc-${Math.random()}.json`));
		const relay = new RelayAgent({ submit: async () => ({}), store });
		relay.enqueue({ text: 'early', taskId: 'task-early', request: 'check PR 1' });
		assert.equal(relay.isInFlight('task-early'), true);
		const injected: string[] = [];
		const q = relay.attachDelivery({ canInject: () => true, inject: (t) => { injected.push(t); return true; }, fallback: () => {}, sleep: async () => {}, turnTimeoutMs: 1_000 });
		await tick(0);
		assert.equal(injected.length, 1);
		assert.match(injected[0], /This answers the user's request: "check PR 1"/);
		q.onTurnEnd();
		await tick(0);
		assert.equal(store.get('task-early')?.delivery, 'spoken');
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

	it('keeps a row per voice task: text, submission, cancel asked, delivery', () => {
		const st = createVoiceTaskStore(join(TMP, 'state', 'rows.json'));
		st.add('task-r', 'check PR 3509');
		st.markCancelRequested('task-r');
		st.set('task-r', 'dm');
		const row = st.get('task-r')!;
		assert.equal(row.text, 'check PR 3509');
		assert.equal(typeof row.submittedAt, 'number');
		assert.equal(row.cancelRequested, true);
		assert.equal(row.delivery, 'dm');
	});
});

describe('the relay agent\'s rule: every voice task ends in an outcome the user heard', () => {
	const base = { core: 'done' as const, settledResult: true, resultIsSkip: false, inFlight: false, now: 1_000_000 };
	it('a result the user has not heard is owed; one they heard, or one being spoken, is not', () => {
		assert.equal(planReconcile({ ...base, row: { at: 1, delivery: 'dm' } }), 'speak_result');
		assert.equal(planReconcile({ ...base, row: { at: 1 } }), 'speak_result', 'no delivery recorded at all');
		assert.equal(planReconcile({ ...base, row: { at: 1, delivery: 'spoken' } }), 'none');
		assert.equal(planReconcile({ ...base, row: { at: 1, delivery: 'injected' } }), 'none');
		assert.equal(planReconcile({ ...base, inFlight: true, row: { at: 1, delivery: 'dm' } }), 'none');
	});
	it('a skip-marked result ([deduped: X]) owes nothing itself: its outcome is X\'s', () => {
		assert.equal(planReconcile({ ...base, resultIsSkip: true, row: { at: 1 } }), 'none');
	});
	it('gives up after a bounded number of replays', () => {
		assert.equal(planReconcile({ ...base, row: { at: 1, delivery: 'dm', replays: MAX_REPLAYS } }), 'none');
	});
	it('a task the core has not picked up after a while is reported once', () => {
		const q = { ...base, core: 'queued' as const, settledResult: false };
		assert.equal(planReconcile({ ...q, row: { at: 1, submittedAt: base.now - NOT_PICKED_MS - 1 } }), 'tell_not_picked');
		assert.equal(planReconcile({ ...q, row: { at: 1, submittedAt: base.now - 1_000 } }), 'none', 'not yet');
		assert.equal(planReconcile({ ...q, row: { at: 1, submittedAt: 0, notPickedNoticed: true } }), 'none', 'once');
		assert.equal(planReconcile({ ...q, row: { at: 1, submittedAt: 0, cancelRequested: true } }), 'none');
	});
});

describe('reconcile pass (task-bridge, temp workspace)', () => {
	const owed: Array<{ text: string; note?: string; taskId?: string }> = [];
	const deliver = (text: string, note: string | undefined, meta: { taskId?: string }) => owed.push({ text, note, taskId: meta.taskId });
	let seq = 0;
	/** A voice task that went through work, whose result the core wrote and the watcher already archived. */
	function finished(text: string, result: string): string {
		const id = `task-${1_800_000_400_000 + ++seq}`;
		writeFileSync(join(TMP, 'tasks', 'archive', MONTH, `${id}.txt`), `id: ${id}\nsource: voice\nchannel_id: local-voice\ntask: ${text}\n`);
		writeFileSync(join(TMP, 'results', 'archive', MONTH, `${id}.txt`), result);
		voiceTaskStore.add(id, text);
		return id;
	}
	beforeEach(() => { owed.length = 0; _pendingTasksForTest.clear(); });

	it('three PR results that all fell back to the DM are all handed over again once the session can speak', () => {
		const ids = ['3509', '5140', '5167'].map((pr) => finished(`check PR ${pr}`, `PR ${pr} status.`));
		for (const id of ids) voiceTaskStore.set(id, 'dm');
		reconcileVoiceTasks(deliver, () => false);
		assert.deepEqual(owed.map((o) => o.taskId).sort(), [...ids].sort());
		assert.ok(owed.every((o) => o.note === MISSED_RESULT_NOTE));
		reconcileVoiceTasks(deliver, (id) => ids.includes(id));
		assert.equal(owed.length, 3, 'nothing again while they are in the queue');
	});

	it('a result forwarded to the DM while voice was offline (the real forward) is owed', async () => {
		const id = finished('check PR 3509', 'PR 3509 has two approvals and green CI.');
		const forwarded = await _forwardOfflineThenArchive(id, `${id}.txt`, 'PR 3509 has two approvals and green CI.', false, async () => {}, 0);
		assert.equal(forwarded, true);
		reconcileVoiceTasks(deliver, () => false);
		assert.deepEqual(owed.filter((o) => o.taskId === id), [{ text: 'PR 3509 has two approvals and green CI.', note: MISSED_RESULT_NOTE, taskId: id }]);
	});

	it('a result already heard owes nothing, also when the row comes back from disk after a restart', () => {
		const id = finished('check PR 4000', 'PR 4000 is merged.');
		createVoiceTaskStore(join(TMP, 'state', 'voice-tasks.json')).set(id, 'spoken');
		reconcileVoiceTasks(deliver, () => false);
		assert.ok(!owed.some((o) => o.taskId === id));
	});

	it('a result still landing in results/ is left to the watcher', () => {
		const id = `task-${1_800_000_500_000 + ++seq}`;
		writeFileSync(join(TMP, 'tasks', `${id}.txt`), `id: ${id}\nsource: voice\ntask: check PR 4100\n`);
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'PR 4100 is open.');
		voiceTaskStore.add(id, 'check PR 4100');
		reconcileVoiceTasks(deliver, () => false);
		assert.ok(!owed.some((o) => o.taskId === id));
		rmSync(join(TMP, 'results', `${id}.txt`));
		rmSync(join(TMP, 'tasks', `${id}.txt`));
	});

	it('a repeated request answered [deduped: X] owes nothing itself; X\'s unheard result is owed', () => {
		const x = finished('check PR 3509', 'PR 3509 is open.');
		voiceTaskStore.set(x, 'dm');
		const y = finished('check PR 3509 again', `[deduped: ${x}]`);
		reconcileVoiceTasks(deliver, () => false);
		assert.ok(owed.some((o) => o.taskId === x));
		assert.ok(!owed.some((o) => o.taskId === y));
	});

	it('a task the core has not picked up is reported as a framed notice, not as a finished task', () => {
		const id = `task-${1_800_000_700_000 + ++seq}`;
		writeFileSync(join(TMP, 'tasks', `${id}.txt`), `id: ${id}\nsource: voice\ntask: draw a kite\n`);
		voiceTaskStore.add(id, 'draw a kite');
		const got: Array<{ text: string; framed?: boolean }> = [];
		reconcileVoiceTasks((text, _note, meta) => got.push({ text, framed: meta.framed }), () => false, Date.now() + NOT_PICKED_MS + 1_000);
		const notice = got.find((g) => g.text.includes('draw a kite'));
		assert.ok(notice?.framed, 'framed, so the queue does not wrap it as a task result');
		assert.match(notice!.text, /^\[System: The user's task "draw a kite" has not been picked up by the core/);
		assert.doesNotMatch(frameBatch([{ text: notice!.text, framed: true }]), /Task completed/);
		rmSync(join(TMP, 'tasks', `${id}.txt`));
	});

	it('a task whose offline DM copy is still in results/ is not handed over again (the drain speaks it)', () => {
		const id = `task-${1_800_000_800_000 + ++seq}`;
		writeFileSync(join(TMP, 'tasks', 'archive', MONTH, `${id}.txt`), `id: ${id}\nsource: voice\ntask: check PR 9\n`);
		writeFileSync(join(TMP, 'results', 'archive', MONTH, `${id}.txt`), 'PR 9 status.');
		voiceTaskStore.add(id, 'check PR 9');
		voiceTaskStore.set(id, 'dm');
		const copy = join(TMP, 'results', `proactive-result-${id}-1800000800.txt`);
		writeFileSync(copy, 'PR 9 status.');
		const got: string[] = [];
		reconcileVoiceTasks((text) => got.push(text), () => false);
		assert.deepEqual(got.filter((t) => t.includes('PR 9')), [], 'the live copy is on its way');
		rmSync(copy);
		reconcileVoiceTasks((text) => got.push(text), () => false);
		assert.equal(got.filter((t) => t.includes('PR 9')).length, 1, 'once the copy is gone, it is owed');
	});

	it('status rows and the count ahead come from the table: a health check in tasks/ is not one of the user\'s', () => {
		writeFileSync(join(TMP, 'tasks', 'task-health-9.txt'), 'id: task-health-9\nsource: health-check\ntask: health\n');
		const a = `task-${1_800_000_600_000 + ++seq}`;
		const b = `task-${1_800_000_600_000 + ++seq}`;
		for (const [id, t] of [[a, 'draw a dog'], [b, 'draw a cat']]) {
			writeFileSync(join(TMP, 'tasks', `${id}.txt`), `id: ${id}\nsource: voice\ntask: ${t}\n`);
			voiceTaskStore.add(id, t);
		}
		assert.equal(voiceTasksAhead(b), 1, 'only the dog is ahead of the cat');
		const rows = voiceTaskRows().filter((r) => r.id === a || r.id === b);
		assert.deepEqual(rows.map((r) => r.state), ['queued', 'queued']);
		assert.ok(!voiceTaskRows().some((r) => r.id === 'task-health-9'));
		for (const f of [a, b, 'task-health-9']) rmSync(join(TMP, 'tasks', `${f}.txt`));
	});
});

describe('voice runs every work call through the relay agent', () => {
	it('work is a background tool mapped to the relay agent subagent, which owns the result queue', () => {
		assert.equal(tb.workTool.execution, 'background');
		assert.equal(tb.workTool.pendingMessage, undefined, 'the status is the tool result, not a pending message');
		const voice = readFileSync(join(import.meta.dirname ?? '.', '..', 'src/voice-agent.ts'), 'utf-8');
		assert.match(voice, /subagentConfigs: \{ work: relayAgentSubagentConfig\(relayAgent\) \},/);
		assert.match(voice, /wireDurableChannels\(session, \{ [^}]*relay: relayAgent \}\);/);
		const runtime = readFileSync(join(import.meta.dirname ?? '.', '..', 'src/live-agent-runtime.ts'), 'utf-8');
		assert.match(runtime, /const results = opts\.relay\.attachDelivery\(\{/);
		assert.match(runtime, /inject: \(text\) => session\.tryPublishSystemNotification\(text\),/);
	});
});

describe('every status answer comes from the one table', () => {
	const src = (f: string) => readFileSync(join(import.meta.dirname, '..', 'src', f), 'utf-8');
	const block = (text: string, start: string) => text.slice(text.indexOf(start), text.indexOf('\n};', text.indexOf(start)));
	it('get_task_status, get_core_status and the cancel list read voiceTaskRows, never tasks/ or the core queue depth', () => {
		for (const [file, start] of [['voice-agent.ts', "name: 'get_task_status'"], ['inline-tools.ts', "name: 'get_core_status'"]] as const) {
			const b = block(src(file), start);
			assert.ok(b.includes('voiceTaskRows()'), `${start} reads the table`);
			assert.ok(!/readdirSync|readQueueDepth|getPendingToolCalls/.test(b), `${start} does not count files or tool calls`);
			assert.ok(!b.includes('inProgress:'), `${start} returns states, not a yes/no in-progress flag`);
		}
		const cancelList = src('inline-tools.ts').split('// list mode:')[1].split('// Targeting:')[0];
		assert.ok(cancelList.includes('voiceTaskRows()') && !cancelList.includes('readdirSync'));
	});

	it('describeVoiceTasks groups the user\'s tasks by where each stands and whether the result was heard', async () => {
		const { describeVoiceTasks } = await import('../src/inline-tools.js');
		const row = (text: string, state: string, delivery?: string) => ({ id: text, text, submittedAt: 1, state, delivery }) as never;
		const out = describeVoiceTasks([row('check PR 5250', 'started'), row('check PR 5259', 'queued'), row('check PR 3509', 'done', 'dm'), row('check PR 5140', 'done', 'spoken')]);
		assert.match(out, /1 in progress \("check PR 5250"\)/);
		assert.match(out, /1 queued \("check PR 5259"\)/);
		assert.match(out, /1 done but the user has not heard the result yet \("check PR 3509"\)/);
		assert.match(out, /1 done and already told to the user \("check PR 5140"\)/);
		assert.match(describeVoiceTasks([]), /no tasks/);
	});
});
