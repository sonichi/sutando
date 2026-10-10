// The relay agent: the work subagent that returns each task's result to its call, the voice task
// table (a durable record of how each result reached the user), and the reconcile rule over it.
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

const { RelayAgent, relayAgentSubagentConfig, createVoiceTaskStore, planReconcile, NOT_PICKED_MS, MAX_REPLAYS } = await import('../src/relay-agent.js');
const { frameTaskResult } = await import('../src/inject-framing.js');
const tb = await import('../src/task-bridge.js');
const { _pendingTasksForTest, voiceTaskStore, MISSED_RESULT_NOTE, _forwardOfflineThenArchive, reconcileVoiceTasks, voiceTaskRows, voiceTasksAhead } = tb;

after(() => rmSync(TMP, { recursive: true, force: true }));

const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe('relay agent subagent', () => {
	function agent(submitted: Record<string, unknown>) {
		const store = createVoiceTaskStore(join(TMP, `relay-${Math.random()}.json`));
		const notices: string[] = [];
		const relay = new RelayAgent({ submit: async () => submitted, store, notice: (t) => notices.push(t) });
		return { relay, store, notices };
	}

	it('runs as the persistent work subagent', async () => {
		const { relay } = agent({ status: 'pending', taskId: 'task-1' });
		const config = relayAgentSubagentConfig(relay);
		assert.equal(config.lifetime, 'persistent_session');
		assert.equal(await config.persistentFactory!('relay-agent', config), relay);
	});

	it('a work call returns the core result for its task, framed, and records it handed over', async () => {
		const { relay, store } = agent({ status: 'pending', taskId: 'task-2', queuedAhead: 0, watcherOnline: true, message: 'm' });
		const call = relay.invoke('Execute tool: work', { task: 'draw a cat' });
		await tick(0);
		assert.equal(relay.isWaiting('task-2'), true);
		assert.equal(relay.offerResult('task-other', 'x'), false, 'a result for another task is not this call\'s');
		assert.equal(relay.offerResult('task-2', 'a cat'), true);
		assert.equal(await call, frameTaskResult('a cat'));
		assert.equal(store.get('task-2')?.delivery, 'injected');
		assert.equal(relay.offerResult('task-2', 'a cat'), false, 'once only');
	});

	it('a status answer (rejected, duplicate, fast path) is returned at once', async () => {
		const { relay } = agent({ status: 'duplicate', taskId: 'task-3', message: 'already pending' });
		assert.match(await relay.invoke('Execute tool: work', { task: 'x' }), /"status":"duplicate"/);
		assert.equal(relay.isWaiting('task-3'), false);
	});

	it('the queue position or an offline core is said at submission; the ordinary case says nothing extra', async () => {
		const ahead = agent({ status: 'pending', taskId: 'task-4', queuedAhead: 2, watcherOnline: true, message: 'Got it, 2 in line.' });
		void ahead.relay.invoke('w', {}).catch(() => {});
		await tick(0);
		assert.equal(ahead.notices.length, 1);
		assert.match(ahead.notices[0], /Got it, 2 in line\./);
		const plain = agent({ status: 'pending', taskId: 'task-5', queuedAhead: 0, watcherOnline: true, message: 'm' });
		void plain.relay.invoke('w', {}).catch(() => {});
		await tick(0);
		assert.equal(plain.notices.length, 0);
	});

	it('an aborted call leaves the task to the ordinary path: no waiter, nothing recorded', async () => {
		const { relay, store } = agent({ status: 'pending', taskId: 'task-6', queuedAhead: 0, watcherOnline: true });
		const ctl = new AbortController();
		const call = relay.invoke('w', {}, ctl.signal);
		await tick(0);
		ctl.abort();
		await assert.rejects(call, /aborted/);
		assert.equal(relay.offerResult('task-6', 'late'), false);
		assert.equal(store.get('task-6')?.delivery, undefined);
	});

	it('dispose ends every waiting call', async () => {
		const { relay } = agent({ status: 'pending', taskId: 'task-7', queuedAhead: 0, watcherOnline: true });
		const call = relay.invoke('w', {});
		await tick(0);
		await relay.dispose();
		await assert.rejects(call, /disposed/);
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
		assert.ok(notice?.framed, 'framed, so delivery does not wrap it as a task result');
		assert.match(notice!.text, /^\[System: The user's task "draw a kite" has not been picked up by the core/);
		assert.doesNotMatch(notice!.text, /Task completed/);
		rmSync(join(TMP, 'tasks', `${id}.txt`));
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
	it('work is a background tool mapped to the relay agent subagent, and results reach it first', async () => {
		const { workTool, WORK_PENDING_MESSAGE } = tb;
		assert.equal(workTool.execution, 'background');
		assert.equal(workTool.pendingMessage, WORK_PENDING_MESSAGE);
		const voice = readFileSync(join(import.meta.dirname ?? '.', '..', 'src/voice-agent.ts'), 'utf-8');
		assert.match(voice, /subagentConfigs: \{ work: relayAgentSubagentConfig\(relayAgent\) \},/);
		assert.match(voice, /wireDurableChannels\(session, \{ [^}]*relay: relayAgent \}\);/);
		const runtime = readFileSync(join(import.meta.dirname ?? '.', '..', 'src/live-agent-runtime.ts'), 'utf-8');
		assert.match(runtime, /if \(meta\?\.taskId && opts\.relay\?\.offerResult\(meta\.taskId, result, deliveryNote\)\)/);
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
