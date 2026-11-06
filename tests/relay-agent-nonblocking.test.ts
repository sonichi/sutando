/**
 * The `work` call is NON_BLOCKING and stays open until the core's result returns through it
 * (gemini-3.8-live native tool calls). Pins the three things that make
 * that safe in sutando:
 *   1. the tool's declaration: `behavior: 'NON_BLOCKING'`, no `pendingMessage` (a pending message
 *      makes bodhi answer the call at once and inject the real result as unrelated text);
 *   2. a call the model re-issues after its result was returned is closed quietly, never resent;
 *   3. a fresh dial (bodhi's recoverUpstream, published as session.reconnectBoundary) ends every open
 *      call, since bodhi's dial-generation fence drops a reply to a call from the abandoned connection;
 *      a resume keeps the calls and their replies are spoken in context.
 *
 * Run: npx tsx --test tests/relay-agent-nonblocking.test.ts
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { RelayAgent, createVoiceTaskStore, RECENT_ANSWER_WINDOW_MS } from '../src/relay-agent.js';
import type { WorkRequestCheck } from '../src/work-request-check.js';

test('work tool: NON_BLOCKING background tool with no pending message', async () => {
	const { workTool } = await import('../src/task-bridge.js');
	assert.equal(workTool.execution, 'background');
	assert.equal(workTool.behavior, 'NON_BLOCKING');
	assert.equal(workTool.pendingMessage, undefined, 'a pendingMessage closes the call id at once; the result must return through the open call');
});

function agent(opts: { check?: WorkRequestCheck; submit?: (args: Record<string, unknown>) => Promise<{ status: string; taskId?: string }> } = {}) {
	const store = createVoiceTaskStore();
	const submitted: string[] = [];
	const logs: string[] = [];
	const notices: string[] = [];
	const relay = new RelayAgent({
		notice: (t) => notices.push(t),
		store,
		submit: opts.submit ?? (async (args) => {
			const id = `task-${submitted.length + 1}`;
			submitted.push(String(args.task));
			store.add(id, String(args.task));
			return { status: 'pending', taskId: id };
		}),
		check: opts.check,
		log: (m) => logs.push(m),
	});
	return { relay, store, submitted, logs, notices };
}

test('a call re-issued after its result was returned is closed quietly and never resent', async () => {
	const { relay, store, submitted, logs, notices } = agent({
		// The check compares the new request with open AND recently answered tasks.
		check: async ({ task, openTasks, answeredTasks }) => {
			const same = [...openTasks, ...(answeredTasks ?? [])].find((t) => t.text === task);
			return same ? { action: 'duplicate', of: same.id } : { action: 'submit' };
		},
	});
	// First call: submitted, waits, and gets its result through the call.
	const first = relay.invoke('work', { task: 'find the apple' });
	await new Promise((r) => setImmediate(r));
	assert.deepEqual(submitted, ['find the apple']);
	assert.equal(relay.isWaiting('task-1'), true);
	assert.equal(relay.offerResult({ taskId: 'task-1', text: 'the apple is red' }), true);
	assert.match(await first, /the apple is red/);
	assert.equal(store.get('task-1')?.delivery, 'injected');

	// The model re-issues the same call before saying the result: closed, not resent, told to say the result now.
	const acknowledged = notices.length;
	const again = await relay.invoke('work', { task: 'find the apple' });
	assert.equal(notices.length, acknowledged, 'no "on it" for something already answered');
	assert.deepEqual(submitted, ['find the apple'], 'the core must not see the request twice');
	assert.match(again, /already came back through its earlier call/);
	assert.match(again, /Say it to the user now/);
	assert.ok(logs.some((l) => l.includes('asked again before its result was said')), logs.join('\n'));
	// Said, and re-issued with no user turn since: an orphan, closed quietly.
	store.set('task-1', 'spoken');
	const orphan = await relay.invoke('work', { task: 'find the apple' });
	assert.match(orphan, /Stay silent/);
	assert.deepEqual(submitted, ['find the apple']);
	// Said, and the user asked again afterwards ("check it again"): a new request, sent.
	relay.onUserTurn('find the apple again', Date.now() + 1);
	const repeat = relay.invoke('work', { task: 'find the apple' });
	await new Promise((r) => setImmediate(r));
	assert.deepEqual(submitted, ['find the apple', 'find the apple'], 'a deliberate repeat reaches the core');
	assert.ok(logs.some((l) => l.includes('asked again after a user turn; sent as a new request')));
	relay.endAllCalls('continuing');
	await repeat;
});

test('recentlyAnswered lists delivered tasks only inside the window', () => {
	let now = 1_000_000;
	const store = createVoiceTaskStore(() => now);
	const relay = new RelayAgent({ store, submit: async () => ({ status: 'pending' }) });
	store.add('old', 'old request'); store.set('old', 'spoken');
	now += RECENT_ANSWER_WINDOW_MS + 1;
	store.add('fresh', 'fresh request'); store.set('fresh', 'injected');
	store.add('open', 'still open');
	assert.deepEqual(relay.recentlyAnswered(now).map((t) => t.id), ['fresh']);
	assert.deepEqual(relay.openTasks(now).map((t) => t.id), ['open']);
});

test('a reconnect ends every open call; the results then go another way', async () => {
	const { relay, logs } = agent();
	const a = relay.invoke('work', { task: 'draw a picture' });
	const b = relay.invoke('work', { task: 'what is the weather' });
	await new Promise((r) => setImmediate(r));
	assert.equal(relay.isWaiting('task-1'), true);
	assert.equal(relay.isWaiting('task-2'), true);

	const ended = relay.endAllCalls('continuing');
	assert.deepEqual(ended.sort(), ['task-1', 'task-2']);
	assert.match(await a, /still running; its result will come in a separate message/);
	assert.match(await b, /still running; its result will come in a separate message/);
	assert.equal(relay.isWaiting('task-1'), false);
	// A result that lands now is NOT returned to the dead call: the caller delivers it elsewhere.
	assert.equal(relay.offerResult({ taskId: 'task-1', text: 'here is the picture' }), false);
	assert.equal(relay.endAllCalls('continuing').length, 0, 'idempotent');
	void logs;
});

test('a fresh dial while a call is still in its checks ends that call before it waits; a resume does not', async () => {
	const { relay, submitted } = agent({ check: async () => { await new Promise((r) => setTimeout(r, 20)); return { action: 'submit' }; } });
	const call = relay.invoke('work', { task: 'check PR 1' });
	await new Promise((r) => setTimeout(r, 5));
	assert.deepEqual(relay.noteBoundary(), [], 'nothing waits yet');
	assert.match(await call, /still running; its result will come in a separate message/, 'a call id from the old connection never waits');
	assert.deepEqual(submitted, ['check PR 1'], 'the request itself was sent');
	assert.equal(relay.isWaiting('task-1'), false);
	const later = relay.invoke('work', { task: 'check PR 2' });
	await new Promise((r) => setTimeout(r, 40));
	assert.equal(relay.isWaiting('task-2'), true, 'a call begun after the boundary waits as usual');
	relay.endAllCalls('continuing');
	await later;
});

test('every reply of a call waits until the session is ACTIVE', async () => {
	let active = false;
	const resumes: Array<() => void> = [];
	const whenActive = () => (active ? Promise.resolve() : new Promise<void>((r) => resumes.push(r)));
	const store = createVoiceTaskStore();
	const relay = new RelayAgent({ store, whenActive, submit: async () => { store.add('task-1', 'x'); return { status: 'pending', taskId: 'task-1' }; } });
	const call = relay.invoke('work', { task: 'x' });
	await new Promise((r) => setImmediate(r));
	assert.equal(relay.offerResult({ taskId: 'task-1', text: 'the answer' }), true);
	let settled = false;
	void call.then(() => { settled = true; });
	await new Promise((r) => setTimeout(r, 20));
	assert.equal(settled, false, 'held while the connection is being replaced');
	active = true;
	for (const r of resumes.splice(0)) r();
	assert.match(await call, /the answer/, 'delivered once ACTIVE');
});

test('an open call returns its result as that call\'s own response (no fake first answer)', async () => {
	const { relay } = agent();
	let settled = false;
	const call = relay.invoke('work', { task: 'summarize the paper' }).then((r) => { settled = true; return r; });
	await new Promise((r) => setTimeout(r, 20));
	assert.equal(settled, false, 'the call stays open while the core works');
	assert.equal(relay.offerResult({ taskId: 'task-1', text: 'three findings' }), true);
	const text = await call;
	assert.equal(settled, true);
	assert.match(text, /TASK_RESULT_START/);
	assert.match(text, /three findings/);
});

test('a submitted request is acknowledged by a separate notice; the call stays open', async () => {
	const notices: string[] = [];
	const store = createVoiceTaskStore();
	const relay = new RelayAgent({
		store,
		submit: async () => { store.add('task-ack', 'draw a cat'); return { status: 'pending', taskId: 'task-ack', queuedAhead: 0, watcherOnline: true, message: 'ignored' }; },
		notice: (t) => notices.push(t),
	});
	let settled = false;
	const call = relay.invoke('work', { task: 'draw a cat' }).then((r) => { settled = true; return r; });
	await new Promise((r) => setTimeout(r, 20));
	assert.equal(notices.length, 1, 'one acknowledgement');
	assert.match(notices[0], /Tell the user in one short sentence that you are on it/);
	assert.equal(settled, false, 'the acknowledgement does not close the call');
	relay.offerResult({ taskId: 'task-ack', text: 'the cat is drawn' });
	assert.match(await call, /the cat is drawn/);
});

test('no "on it" when the check asks the user; a duplicate of an open task is told it is on its way; nothing said when the call is aborted or the core is offline', async () => {
	for (const verdict of [{ action: 'ask', question: 'Which PR?' }, { action: 'duplicate', of: 'task-open' }] as const) {
		const notices: string[] = [];
		const store = createVoiceTaskStore();
		store.add('task-open', 'check PR');
		const relay = new RelayAgent({ store, submit: async () => ({ status: 'pending', taskId: 'task-x' }), notice: (t) => notices.push(t), check: async () => verdict });
		void relay.invoke('work', { task: 'check the PR' });
		await new Promise((r) => setTimeout(r, 20));
		if (verdict.action === 'ask') assert.equal(notices.length, 0, 'ask: nothing said to be "on it"');
		else { assert.equal(notices.length, 1); assert.match(notices[0], /already sent to the core and its result will be told/); }
		relay.endAllCalls('continuing');
	}
	// Aborted during submit: the call ends with "continuing" and no acknowledgement follows it.
	{
		const notices: string[] = [];
		const ac = new AbortController();
		const relay = new RelayAgent({ store: createVoiceTaskStore(), notice: (t) => notices.push(t), submit: async () => { ac.abort(); return { status: 'pending', taskId: 'task-a' }; } });
		const out = await relay.invoke('work', { task: 'x' }, ac.signal);
		assert.match(out, /still running/);
		assert.equal(notices.length, 0, 'an aborted call is not acknowledged');
	}
	// The core is offline (status not pending): the bridge's own message is the answer, no acknowledgement.
	{
		const notices: string[] = [];
		const relay = new RelayAgent({ store: createVoiceTaskStore(), notice: (t) => notices.push(t), submit: async () => ({ status: 'offline', message: 'saved for later' }) });
		const out = await relay.invoke('work', { task: 'x' });
		assert.match(out, /offline/);
		assert.equal(notices.length, 0);
	}
});

test('a queued request is acknowledged with its queue position instead', async () => {
	const notices: string[] = [];
	const store = createVoiceTaskStore();
	const relay = new RelayAgent({
		store,
		submit: async () => { store.add('task-q', 'draw a dog'); return { status: 'pending', taskId: 'task-q', queuedAhead: 1, watcherOnline: true, message: 'Tell the user exactly "Got it, right after the one I\'m on."' }; },
		notice: (t) => notices.push(t),
	});
	void relay.invoke('work', { task: 'draw a dog' });
	await new Promise((r) => setTimeout(r, 20));
	assert.equal(notices.length, 1, 'the queue position instead of the plain acknowledgement');
	assert.match(notices[0], /right after the one I'm on/);
	relay.endAllCalls('continuing');
});
