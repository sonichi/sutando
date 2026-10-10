// The relay agent: the work subagent whose async call returns its task's result, and the voice task
// table (how each result reached the user, which result answered it) with the reconcile rule over it.
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

const { RelayAgent, relayAgentSubagentConfig, createVoiceTaskStore, frameResult, planReconcile, NOT_PICKED_MS, MAX_REPLAYS } = await import('../src/relay-agent.js');
const { frameTaskResult } = await import('../src/inject-framing.js');
const tb = await import('../src/task-bridge.js');
const { _pendingTasksForTest, voiceTaskStore, MISSED_RESULT_NOTE, _forwardOfflineThenArchive, reconcileVoiceTasks, voiceTaskRows, voiceTasksAhead } = tb;

after(() => rmSync(TMP, { recursive: true, force: true }));

const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe('relay agent subagent: the work call returns its task\'s result', () => {
	function agent(submitted: Record<string, unknown>) {
		const store = createVoiceTaskStore(join(TMP, `relay-${Math.random()}.json`));
		const notices: string[] = [];
		const relay = new RelayAgent({ submit: async () => submitted, store, notice: (t) => notices.push(t) });
		return { relay, store, notices };
	}
	const pending = (taskId: string, extra: Record<string, unknown> = {}) => ({ status: 'pending', taskId, queuedAhead: 0, watcherOnline: true, message: 'm', ...extra });

	it('runs as the persistent work subagent', async () => {
		const { relay } = agent({});
		const config = relayAgentSubagentConfig(relay);
		assert.equal(config.lifetime, 'persistent_session');
		assert.equal(await config.persistentFactory!('relay-agent', config), relay);
	});

	it('the call waits for its result and returns it, naming the request; the row is recorded handed over', async () => {
		const { relay, store } = agent(pending('task-2'));
		const call = relay.invoke('Execute tool: work', { task: 'draw a cat' });
		await tick(0);
		assert.equal(relay.isWaiting('task-2'), true);
		assert.equal(relay.offerResult({ text: 'x', taskId: 'task-other' }), false, 'another task\'s result is not this call\'s');
		assert.equal(relay.offerResult({ text: 'a cat', taskId: 'task-2', requests: ['draw a cat'] }), true);
		const out = await call;
		assert.match(out, /This answers the user's request: "draw a cat"/);
		assert.ok(out.includes(frameTaskResult('a cat')));
		assert.equal(store.get('task-2')?.delivery, 'injected');
		assert.equal(relay.offerResult({ text: 'a cat', taskId: 'task-2' }), false, 'once only');
	});

	it('a status answer (duplicate, rejected, fast path) returns at once', async () => {
		const { relay } = agent({ status: 'duplicate', taskId: 'task-3', message: 'already pending' });
		assert.match(await relay.invoke('w', { task: 'x' }), /"status":"duplicate"/);
		assert.equal(relay.isWaiting('task-3'), false);
	});

	it('a queue position or an offline core is said at submission; the ordinary case says nothing extra', async () => {
		const ahead = agent(pending('task-4', { queuedAhead: 2, message: 'Got it, 2 in line.' }));
		void ahead.relay.invoke('w', {}).catch(() => {});
		await tick(0);
		assert.deepEqual(ahead.notices.length, 1);
		assert.match(ahead.notices[0], /Got it, 2 in line\./);
		const plain = agent(pending('task-5'));
		void plain.relay.invoke('w', {}).catch(() => {});
		await tick(0);
		assert.equal(plain.notices.length, 0);
	});

	it('an aborted call leaves the result to be delivered another way', async () => {
		const { relay, store } = agent(pending('task-6'));
		const ctl = new AbortController();
		const call = relay.invoke('w', {}, ctl.signal);
		await tick(0);
		ctl.abort();
		await assert.rejects(call, /aborted/);
		assert.equal(relay.offerResult({ text: 'late', taskId: 'task-6' }), false);
		assert.equal(store.get('task-6')?.delivery, undefined);
	});

	it('a result answering several tasks ends their waiting calls too, and records them all', async () => {
		const store = createVoiceTaskStore(join(TMP, `multi-${Math.random()}.json`));
		const ids = ['task-a', 'task-b', 'task-c'];
		let n = 0;
		const relay = new RelayAgent({ submit: async () => pending(ids[n++]), store });
		const calls = ids.map(() => relay.invoke('w', {}));
		await tick(0);
		assert.equal(relay.offerResult({ text: 'All three.', taskId: 'task-a', requests: ['r1', 'r2', 'r3'], alsoFor: ['task-b', 'task-c'] }), true);
		const [a, b, c] = await Promise.all(calls);
		assert.match(a, /This answers 3 of the user's requests/);
		assert.match(b, /answered_together/);
		assert.match(c, /"answeredIn":"task-a"/);
		assert.deepEqual(ids.map((id) => store.get(id)?.delivery), ['injected', 'injected', 'injected']);
	});

	it('dispose ends every waiting call', async () => {
		const { relay } = agent(pending('task-8'));
		const call = relay.invoke('w', {});
		await tick(0);
		await relay.dispose();
		await assert.rejects(call, /disposed/);
	});
});

describe('frameResult', () => {
	it('one result is framed exactly as before; a note sits outside the result markers', () => {
		assert.equal(frameResult({ text: 'x' }), frameTaskResult('x'));
		assert.match(frameResult({ text: 'x', note: 'n' }), /TASK_RESULT_END[\s\S]*n/);
	});
	it('a framed item is not wrapped as a task result', () => {
		assert.equal(frameResult({ text: '[System: call done]', framed: true }), '[System: call done]');
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

describe('a result that answers several requests (the core deduped them into it)', () => {
	it('the table records which result answered a task, and lists the tasks a result answered', () => {
		const store = createVoiceTaskStore(join(TMP, `ab-${Math.random()}.json`));
		store.add('task-a', 'check PR 5308');
		store.add('task-b', 'check PR 5309');
		store.add('task-c', 'check PR 5310');
		store.setAnsweredBy('task-b', 'task-a');
		store.setAnsweredBy('task-c', 'task-a');
		store.setAnsweredBy('task-a', 'task-a');
		assert.equal(store.get('task-b')?.answeredBy, 'task-a');
		assert.equal(store.get('task-a')?.answeredBy, undefined, 'never answered by itself');
		assert.deepEqual(store.answeredBy('task-a').map(([id]) => id).sort(), ['task-b', 'task-c']);
	});

	it('a result names every request it answers', () => {
		assert.match(frameResult({ text: 'All three.', requests: ['check PR 5308', 'check PR 5309', 'check PR 5310'] }), /This answers 3 of the user's requests: "check PR 5308"; "check PR 5309"; "check PR 5310"\. Cover each of them\./);
	});

	it('parses the task a [deduped: …] result points to', async () => {
		const { dedupTarget } = await import('../src/skip_marker_ownership.js');
		assert.equal(dedupTarget('[deduped: task-1791611250258]'), 'task-1791611250258');
		assert.equal(dedupTarget('**[core: 2]**\n[deduped: task-9]'), 'task-9');
		assert.equal(dedupTarget('[no-send]'), null);
		assert.equal(dedupTarget('see [deduped: task-9]'), null);
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
		assert.doesNotMatch(frameResult({ text: notice!.text, framed: true }), /Task completed/);
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

	it('a task the core answered in another task\'s result is owed that result, labelled for it', () => {
		const a = `task-${1_800_000_900_000 + ++seq}`;
		const b = `task-${1_800_000_900_000 + ++seq}`;
		writeFileSync(join(TMP, 'tasks', 'archive', MONTH, `${a}.txt`), `id: ${a}\nsource: voice\ntask: check PR 5308\n`);
		writeFileSync(join(TMP, 'tasks', 'archive', MONTH, `${b}.txt`), `id: ${b}\nsource: voice\ntask: check PR 5309\n`);
		writeFileSync(join(TMP, 'results', 'archive', MONTH, `${a}.txt`), 'All three: 5308 merged, 5309 ready.');
		writeFileSync(join(TMP, 'results', 'archive', MONTH, `${b}.txt`), `[deduped: ${a}]`);
		voiceTaskStore.add(a, 'check PR 5308');
		voiceTaskStore.add(b, 'check PR 5309');
		voiceTaskStore.set(a, 'spoken');      // handed over before the dedup link existed, naming only 5308
		voiceTaskStore.setAnsweredBy(b, a);
		const got: Array<{ text: string; taskId?: string }> = [];
		reconcileVoiceTasks((text, _note, meta) => got.push({ text, taskId: meta.taskId }), () => false);
		const owed = got.filter((g) => g.taskId === b);
		assert.equal(owed.length, 1);
		assert.match(owed[0].text, /5309 ready/);
		assert.equal(got.filter((g) => g.taskId === a).length, 0, 'the answering task itself was heard');
		voiceTaskStore.set(b, 'spoken');
		got.length = 0;
		reconcileVoiceTasks((text, _note, meta) => got.push({ text, taskId: meta.taskId }), () => false);
		assert.equal(got.filter((g) => g.taskId === b).length, 0, 'heard now: owes nothing');
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
	it('work is a background tool mapped to the relay agent subagent; the model hears the pending message at once', () => {
		assert.equal(tb.workTool.execution, 'background');
		assert.equal(tb.workTool.pendingMessage, tb.WORK_PENDING_MESSAGE, 'without it the model says nothing until the result');
		assert.equal(tb.workTool.behavior, undefined);
		const voice = readFileSync(join(import.meta.dirname ?? '.', '..', 'src/voice-agent.ts'), 'utf-8');
		assert.match(voice, /subagentConfigs: \{ work: relayAgentSubagentConfig\(relayAgent\) \},/);
		assert.match(voice, /wireDurableChannels\(session, \{ [^}]*relay: relayAgent \}\);/);
		const runtime = readFileSync(join(import.meta.dirname ?? '.', '..', 'src/live-agent-runtime.ts'), 'utf-8');
		assert.match(runtime, /if \(opts\.relay\.offerResult\(item\)\)/);
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
