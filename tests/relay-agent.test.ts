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

const { RelayAgent, createHandoffPacer, requestMarks, relayAgentSubagentConfig, createVoiceTaskStore, frameResult, planReconcile, NOT_PICKED_MS, MAX_REPLAYS, SAY_WHICH_ONE, SAY_WHICH_EACH, createSpeechGate } = await import('../src/relay-agent.js');
const { frameTaskResult } = await import('../src/inject-framing.js');
const tb = await import('../src/task-bridge.js');
const { _pendingTasksForTest, voiceTaskStore, MISSED_RESULT_NOTE, _forwardOfflineThenArchive, reconcileVoiceTasks, voiceTaskRows, voiceTasksAhead } = tb;

after(() => rmSync(TMP, { recursive: true, force: true }));

const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe('relay agent subagent: the work call returns its task\'s result', () => {
	function agent(submitted: Record<string, unknown>) {
		const store = createVoiceTaskStore();
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

	it('an aborted call ends without a failure (bodhi would read a rejection out as one); the result goes another way', async () => {
		const { relay, store } = agent(pending('task-6'));
		const ctl = new AbortController();
		const call = relay.invoke('w', {}, ctl.signal);
		await tick(0);
		ctl.abort();
		assert.match(await call, /still running; its result will come in a separate message/);
		assert.equal(relay.offerResult({ text: 'late', taskId: 'task-6' }), false);
		assert.equal(store.get('task-6')?.delivery, undefined);
	});

	it('a cancelled task ends its waiting call quietly: the core never answers a cancelled task', async () => {
		const { relay } = agent(pending('task-7'));
		const call = relay.invoke('w', {});
		await tick(0);
		const ended: string[] = [];
		tb.setVoiceTaskEndedListener((id, why) => { ended.push(`${id}:${why}`); relay.endCall(id, why); });
		try {
			tb.noteVoiceTaskCancelled('task-7');
		} finally {
			tb.setVoiceTaskEndedListener(null);
		}
		assert.deepEqual(ended, ['task-7:cancelled']);
		assert.match(await call, /cancelled this task and was already told/);
		assert.doesNotMatch(await call, /fail/i);
		assert.equal(relay.isWaiting('task-7'), false);
		assert.equal(relay.endCall('task-7', 'cancelled'), false, 'nothing left to end');
	});

	it('voice-agent ends the relay call of each task whose result never comes back to voice', () => {
		const src = readFileSync(join(import.meta.dirname, '..', 'src', 'voice-agent.ts'), 'utf-8');
		assert.match(src, /setVoiceTaskEndedListener\(\(taskId, why\) => \{ relayAgent\.endCall\(taskId, why\); \}\);/);
	});

	it('a result answering several tasks ends their waiting calls too, and records them all', async () => {
		const store = createVoiceTaskStore();
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

describe('relay agent: a text model checks a request before it is submitted', () => {
	function agent(check: (input: { task: string; recentConversation: string }) => Promise<{ action: 'submit' } | { action: 'ask'; question: string } | { action: 'duplicate'; of: string }>) {
		const store = createVoiceTaskStore();
		const submitted: string[] = [];
		const logs: string[] = [];
		const relay = new RelayAgent({
			store,
			check,
			recentConversation: () => 'user: 帮我查一下那个 PR 的状态',
			log: (m) => logs.push(m),
			submit: async (args) => { submitted.push(String(args.task)); return { status: 'pending', taskId: 'task-c1', queuedAhead: 0, watcherOnline: true, message: 'm' }; },
		});
		return { relay, submitted, logs };
	}

	it('a request with a blank in it is not submitted: the model is told to ask the user', async () => {
		// 2026-10-12 03:08 and 03:17: "帮我查一下那个 PR 的状态" became the task "查一下PR的状态".
		const seen: Array<{ task: string; recentConversation: string }> = [];
		const { relay, submitted, logs } = agent(async (input) => { seen.push(input); return { action: 'ask', question: '你说的是哪个 PR？' }; });
		// Raced against a timer: with the ask path removed the call waits on a submit that never ends, so the test fails instead of hanging.
		const out = await Promise.race([relay.invoke('w', { task: '查一下PR的状态' }), tick(3_000).then(() => 'timed out')]);
		assert.deepEqual(submitted, [], 'nothing submitted');
		assert.match(out, /Nothing was submitted/);
		assert.match(out, /Ask the user now, in their language, exactly: "你说的是哪个 PR？"/);
		assert.deepEqual(seen, [{ task: '查一下PR的状态', recentConversation: 'user: 帮我查一下那个 PR 的状态', openTasks: [] }], 'the check reads the request, the recent words and the open tasks');
		assert.ok(logs.some((l) => l.includes('not submitted, asking the user: 你说的是哪个 PR？')));
	});

	it('a complete request is submitted as before', async () => {
		const { relay, submitted } = agent(async () => ({ action: 'submit' }));
		void relay.invoke('w', { task: 'check PR 5302' });
		await tick(10);
		assert.deepEqual(submitted, ['check PR 5302']);
	});

	it('a check that fails lets the request through', async () => {
		const { relay, submitted } = agent(async () => { throw new Error('model down'); });
		void relay.invoke('w', { task: 'check PR 5302' });
		await tick(10);
		assert.deepEqual(submitted, ['check PR 5302'], 'never blocks work');
	});

	it('the model\'s reply is read as a decision; anything unclear means submit', async () => {
		const { parseWorkCheck } = await import('../src/work-request-check.js');
		assert.deepEqual(parseWorkCheck('{"action":"ask","question":"Which PR?"}'), { action: 'ask', question: 'Which PR?' });
		assert.deepEqual(parseWorkCheck('```json\n{"action": "ask", "question": "哪个文件？"}\n```'), { action: 'ask', question: '哪个文件？' });
		assert.deepEqual(parseWorkCheck('{"action":"submit"}'), { action: 'submit' });
		assert.deepEqual(parseWorkCheck('{"action":"ask"}'), { action: 'submit' }, 'an ask with no question is not an ask');
		assert.deepEqual(parseWorkCheck('sure, go ahead'), { action: 'submit' });
	});
});

describe('the Gemini check call: every failure lets the request through', () => {
	const realFetch = globalThis.fetch;
	after(() => { globalThis.fetch = realFetch; });
	const input = { task: 'check PR 5302', recentConversation: 'user: check PR 5302', openTasks: [] };
	const reply = (body: unknown, status = 200) => async () => new Response(typeof body === 'string' ? body : JSON.stringify(body), { status });

	it('a model that never answers is cut off at the timeout: work submits, turn check is none', async () => {
		const { createGeminiWorkCheck, createGeminiTurnClassifier } = await import('../src/work-request-check.js');
		globalThis.fetch = ((_url: unknown, init?: RequestInit) => new Promise((_r, reject) => {
			init?.signal?.addEventListener('abort', () => reject(new Error('aborted')), { once: true });
		})) as typeof fetch;
		const logs: string[] = [];
		const t0 = Date.now();
		const work = await createGeminiWorkCheck({ apiKey: () => 'k', timeoutMs: 50, log: (m) => logs.push(m) })(input);
		const turn = await createGeminiTurnClassifier({ apiKey: () => 'k', timeoutMs: 50, log: (m) => logs.push(m) })({ utterance: 'check PR 5302', recentConversation: input.recentConversation, openTasks: [] });
		assert.deepEqual(work, { action: 'submit' });
		assert.deepEqual(turn, { action: 'none' });
		assert.ok(Date.now() - t0 < 1_000, 'the timeout ends the wait');
		assert.ok(logs.some((l) => l.includes('work check failed')) && logs.some((l) => l.includes('turn check failed')));
	});

	it('a call the session aborts ends at once and submits nothing extra', async () => {
		const { createGeminiWorkCheck } = await import('../src/work-request-check.js');
		globalThis.fetch = ((_url: unknown, init?: RequestInit) => new Promise((_r, reject) => {
			init?.signal?.addEventListener('abort', () => reject(new Error('aborted')), { once: true });
		})) as typeof fetch;
		const ctl = new AbortController();
		const p = createGeminiWorkCheck({ apiKey: () => 'k', timeoutMs: 10_000 })(input, ctl.signal);
		ctl.abort();
		assert.deepEqual(await Promise.race([p, tick(1_000).then(() => 'hung')]), { action: 'submit' });
	});

	it('an HTTP error, a non-JSON body, an empty answer and a missing key all submit', async () => {
		const { createGeminiWorkCheck } = await import('../src/work-request-check.js');
		const check = createGeminiWorkCheck({ apiKey: () => 'k', timeoutMs: 1_000 });
		globalThis.fetch = reply({ error: { message: 'quota exceeded' } }, 429) as typeof fetch;
		assert.deepEqual(await check(input), { action: 'submit' }, 'HTTP error');
		globalThis.fetch = reply('<html>bad gateway</html>', 502) as typeof fetch;
		assert.deepEqual(await check(input), { action: 'submit' }, 'non-JSON body');
		globalThis.fetch = reply({ candidates: [] }) as typeof fetch;
		assert.deepEqual(await check(input), { action: 'submit' }, 'empty answer');
		let called = false;
		globalThis.fetch = (async () => { called = true; return new Response('{}'); }) as typeof fetch;
		assert.deepEqual(await createGeminiWorkCheck({ apiKey: () => undefined })(input), { action: 'submit' }, 'no key');
		assert.equal(called, false, 'no key, no call');
	});

	it('a real answer is read, and the key goes in a header, never the URL', async () => {
		const { createGeminiWorkCheck } = await import('../src/work-request-check.js');
		let url = ''; let key = '';
		globalThis.fetch = (async (u: unknown, init?: RequestInit) => {
			url = String(u); key = String((init?.headers as Record<string, string>)['x-goog-api-key']);
			return new Response(JSON.stringify({ candidates: [{ content: { parts: [{ text: '{"action":"ask","question":"哪个 PR？"}' }] } }] }));
		}) as typeof fetch;
		assert.deepEqual(await createGeminiWorkCheck({ apiKey: () => 'secret-k' })(input), { action: 'ask', question: '哪个 PR？' });
		assert.equal(key, 'secret-k');
		assert.ok(!url.includes('secret-k'));
	});
});

describe('relay agent: review of #5348, should-fixes', () => {
	it('the user is asked once per request: the next call\'s answer is sent as it is', async () => {
		const store = createVoiceTaskStore();
		const submitted: string[] = [];
		const relay = new RelayAgent({
			store,
			check: async () => ({ action: 'ask', question: '哪个 PR？' }),
			submit: async (args) => { submitted.push(String(args.task)); return { status: 'pending', taskId: 'task-q1', queuedAhead: 0, watcherOnline: true, message: 'm' }; },
		});
		assert.match(await relay.invoke('w', { task: '查一下那个 PR' }), /Ask the user now/);
		void relay.invoke('w', { task: '查一下最新的那个 PR' });
		await tick(5);
		assert.deepEqual(submitted, ['查一下最新的那个 PR'], 'not asked a second time');
	});

	it('a call bodhi cancels while it waits is not submitted', async () => {
		const store = createVoiceTaskStore();
		const submitted: string[] = [];
		const speech = createSpeechGate({ quietMs: 60, maxHoldMs: 2_000 });
		const relay = new RelayAgent({ store, speech, submit: async (args) => { submitted.push(String(args.task)); return {}; } });
		const ctl = new AbortController();
		speech.onUserSpeechStarted();
		const call = relay.invoke('w', { task: 'check PR 1' }, ctl.signal);
		await tick(10);
		ctl.abort();
		assert.match(await call, /nothing was submitted/);
		assert.deepEqual(submitted, []);
	});

	it('relayCheck in voice-agent.json: absent = on with the default model, false or enabled:false = off, model picks it', async () => {
		const { resolveRelayCheck } = await import('../src/voice-config.js');
		assert.deepEqual(resolveRelayCheck({}, 'm0'), { model: 'm0' });
		assert.equal(resolveRelayCheck({ relayCheck: false }, 'm0'), null);
		assert.equal(resolveRelayCheck({ relayCheck: { enabled: false } }, 'm0'), null);
		assert.deepEqual(resolveRelayCheck({ relayCheck: { model: ' gemini-x ' } }, 'm0'), { model: 'gemini-x' });
		assert.deepEqual(resolveRelayCheck({ relayCheck: 'yes' as never }, 'm0'), { model: 'm0' }, 'malformed means the default');
		const src = (f: string) => readFileSync(join(import.meta.dirname ?? '.', '..', 'src', f), 'utf-8');
		assert.ok(!src('work-request-check.ts').includes('process.env'), 'no undocumented env var');
		assert.ok(src('voice-agent.config.json.example').includes('_comment_relayCheck'));
		assert.ok(src('work-request-check.ts').includes("'x-goog-api-key': opts.apiKey") && !src('work-request-check.ts').includes('?key='), 'the key goes in a header');
	});

	it('the task page is local-only, and a result waiting its turn is not said into a meeting that began meanwhile', () => {
		const src = (f: string) => readFileSync(join(import.meta.dirname ?? '.', '..', 'src', f), 'utf-8');
		assert.match(src('web-client.ts'), /\(url\.pathname === '\/voice-tasks' \|\| url\.pathname === '\/voice-tasks\/view'\) && !LAN_SHARE && !isLoopbackAddress\(req\.socket\.remoteAddress\)/);
		const rt = src('live-agent-runtime.ts');
		assert.match(rt, /hand: \(\) => \{\n\t\t\t\/\/[^\n]*\n\t\t\tif \(meetingHoldsModel\(session\)\) return false;/);
		assert.match(rt, /attempt: \(\) => \{\n\t\t\t\tif \(meetingHoldsModel\(session\)\) \{ holdForMeeting\(item\); return true; \}/);
	});
});

describe('relay agent: a question for the user is said as soon as the model stops speaking', () => {
	it('at once when the model is quiet; after its turn ends when it is speaking; only the newest waits', async () => {
		// 2026-10-13 00:41: decided at :16, said at :35, queued behind the model's answer and a noise turn.
		const { createAskDelivery } = await import('../src/relay-agent.js');
		const said: string[] = [];
		const a = createAskDelivery({ inject: (t) => said.push(t), maxWaitMs: 5_000 });
		a.ask('q1');
		assert.deepEqual(said, ['q1'], 'quiet model: at once');
		a.onModelAudio();
		a.ask('q2');
		a.ask('q3');
		assert.deepEqual(said, ['q1'], 'waits while the model speaks');
		a.onTurnEnd();
		assert.deepEqual(said, ['q1', 'q3'], 'the newest, when the turn ends');
		a.onTurnEnd();
		assert.deepEqual(said, ['q1', 'q3'], 'said once');
	});

	it('a model that keeps speaking does not hold the question past the cap', async () => {
		const { createAskDelivery } = await import('../src/relay-agent.js');
		const said: string[] = [];
		const a = createAskDelivery({ inject: (t) => said.push(t), maxWaitMs: 30 });
		a.onModelAudio();
		a.ask('q');
		await tick(60);
		assert.deepEqual(said, ['q']);
	});

	it('the turn check\'s question goes through the ask channel, not the notification queue', async () => {
		const asked: string[] = [];
		const notices: string[] = [];
		const relay = new RelayAgent({
			store: createVoiceTaskStore(),
			classify: async () => ({ action: 'ask', question: '哪一个 PR？' }),
			ask: (t) => asked.push(t),
			notice: (t) => notices.push(t),
			submit: async () => ({}),
		});
		relay.onUserTurn('帮我查一下那个 PR 的状态');
		await tick(10);
		assert.equal(asked.length, 1);
		assert.match(asked[0], /exactly: "哪一个 PR？"/);
		assert.deepEqual(notices, []);
		const agentSrc = readFileSync(join(import.meta.dirname ?? '.', '..', 'src', 'voice-agent.ts'), 'utf-8');
		assert.match(agentSrc, /inject: \(text\) => \{ void sessionRef\?\.injectText\(text, \{ mode: 'live' \}\); \}/);
		assert.match(agentSrc, /subscribe\('turn\.end', \(\) => relayAsk\.onTurnEnd\(\)\)/);
	});
});

describe('relay agent: the same request is not sent twice', () => {
	it('a work call the check finds already open waits for that task\'s result instead of submitting', async () => {
		// 2026-10-12 22:10: "333PR53" and then "PR5333 Sultando Repo" became two tasks for PR 5333.
		const store = createVoiceTaskStore();
		store.add('task-open', '帮我查一下 PR 5333 改了什么');
		const submitted: string[] = [];
		const seen: unknown[] = [];
		const relay = new RelayAgent({
			store,
			check: async (input) => { seen.push(input.openTasks); return { action: 'duplicate', of: 'task-open' }; },
			submit: async (args) => { submitted.push(String(args.task)); return { status: 'pending', taskId: 'task-x', queuedAhead: 0, watcherOnline: true, message: 'm' }; },
		});
		const call = relay.invoke('w', { task: '查一下苏坦诺仓库 PR 5333 修改的具体内容' });
		await tick(5);
		assert.deepEqual(submitted, [], 'nothing new submitted');
		assert.deepEqual(seen, [[{ id: 'task-open', text: '帮我查一下 PR 5333 改了什么' }]], 'the check saw the open task');
		assert.ok(relay.isWaiting('task-open'), 'this call now waits on the open task');
		assert.equal(relay.offerResult({ taskId: 'task-open', text: 'PR 5333 adds a summary.' }), true);
		assert.match(await call, /PR 5333 adds a summary/);
	});

	it('only an id that is really open counts as a duplicate', async () => {
		const { parseWorkCheck, parseTurnVerdict } = await import('../src/work-request-check.js');
		const open = [{ id: 'task-1', text: 'PR 1' }];
		assert.deepEqual(parseWorkCheck('{"action":"duplicate","of":"task-1"}', open), { action: 'duplicate', of: 'task-1' });
		assert.deepEqual(parseWorkCheck('{"action":"duplicate","of":"task-9"}', open), { action: 'submit' });
		assert.deepEqual(parseTurnVerdict('{"action":"duplicate","of":"task-9"}', open), { action: 'none' });
	});

	it('answered, cancelled, retired or corrected tasks are not open', () => {
		const store = createVoiceTaskStore();
		for (const id of ['a', 'b', 'c', 'd', 'e', 'f']) store.add(`task-${id}`, id);
		store.set('task-b', 'spoken');
		store.markCancelRequested('task-c');
		store.markRetired('task-d');
		store.setSupersededBy('task-e', 'task-a');
		store.set('task-f', 'dm');
		const relay = new RelayAgent({ store, submit: async () => ({}) });
		assert.deepEqual(relay.openTasks().map((t) => t.id), ['task-a']);
	});
});

describe('relay agent: every finished user turn is checked, whatever tool the voice model chose', () => {
	function agent(classify: (input: { utterance: string; openTasks?: unknown[] }) => Promise<unknown>) {
		const store = createVoiceTaskStore();
		const submitted: string[] = [];
		const notices: string[] = [];
		const logs: string[] = [];
		let n = 0;
		const relay = new RelayAgent({
			store,
			classify: classify as never,
			recentConversation: () => 'user: 帮我查一下 5428 改了什么',
			notice: (t) => notices.push(t),
			log: (m) => logs.push(m),
			submit: async (args) => { submitted.push(String(args.task)); const id = `task-t${++n}`; store.add(id, String(args.task)); return { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true, message: 'm' }; },
		});
		return { relay, store, submitted, notices, logs };
	}

	it('a request the voice model took for a task-list question is sent to the core', async () => {
		// 2026-10-12 22:10: "帮我查一下 5334 的状态" went to get_task_status; no work call, the task was lost.
		const { relay, store, submitted, notices } = agent(async () => ({ action: 'work', task: '查一下 PR 5334 的状态' }));
		relay.onUserTurn('帮我查一下 5334 的状态。');
		await tick(10);
		assert.deepEqual(submitted, ['查一下 PR 5334 的状态']);
		assert.equal(store.get('task-t1')?.text, '查一下 PR 5334 的状态', 'it is in the table');
		assert.match(notices[0], /sent to the core for the user: "查一下 PR 5334 的状态".*Do not call work for it/);
	});

	it('a turn the voice model already sent with work is left to that call', async () => {
		let asked = 0;
		const { relay, submitted } = agent(async () => { asked++; return { action: 'work', task: 'x' }; });
		void relay.invoke('w', { task: 'check PR 5428' });
		await tick(1);
		relay.onUserTurn('帮我查一下 5428 改了什么');
		await tick(10);
		assert.equal(asked, 0, 'not even checked');
		assert.deepEqual(submitted, ['check PR 5428'], 'only the voice model\'s own call');
	});

	it('a work call that arrives while the turn is being checked wins; the turn check sends nothing', async () => {
		let release!: () => void;
		const gate = new Promise<void>((r) => { release = r; });
		const { relay, submitted } = agent(async () => { await gate; return { action: 'work', task: 'from the turn check' }; });
		relay.onUserTurn('帮我查一下 5428');
		await tick(1);
		void relay.invoke('w', { task: 'from the voice model' });
		release();
		await tick(10);
		assert.deepEqual(submitted, ['from the voice model']);
	});

	it('a turn the voice model answered with a tool that fetches the answer is not sent to the core', async () => {
		// 2026-10-13 17:08: "open.space launch video 那个 room 里最近发生了什么" was read with read_room and also sent as a task.
		let asked = 0;
		const { relay, submitted, logs } = agent(async () => { asked++; return { action: 'work', task: 'x' }; });
		relay.noteAnsweringTool('read_room');
		relay.onUserTurn('open.space launch video 那个 room 里最近发生了什么');
		await tick(10);
		assert.equal(asked, 0, 'not even checked');
		assert.deepEqual(submitted, []);
		assert.ok(logs.some((l) => l.includes('read_room answers this turn')));
	});

	it('a read that starts while the turn is being checked wins too', async () => {
		let release!: () => void;
		const gate = new Promise<void>((r) => { release = r; });
		const { relay, submitted } = agent(async () => { await gate; return { action: 'work', task: 'from the turn check' }; });
		relay.onUserTurn('Sutando Triage 里 5473 那个 guest line 是怎么回事');
		await tick(1);
		relay.noteAnsweringTool('read_room');
		release();
		await tick(10);
		assert.deepEqual(submitted, []);
	});

	it('the next turn, with no such tool, is checked as usual', async () => {
		const { relay, submitted } = agent(async () => ({ action: 'work', task: '查一下 PR 5473 的 review' }));
		relay.noteAnsweringTool('read_room');
		relay.onUserTurn('Sutando Triage 里有什么新消息');
		await tick(10);
		relay.onUserTurn('帮我查一下 5473 的 review');
		await tick(10);
		assert.deepEqual(submitted, ['查一下 PR 5473 的 review']);
	});

	it('chit-chat and task-list questions send nothing; an ask is put to the user; a repeat is not sent', async () => {
		const verdicts = [{ action: 'none' }, { action: 'ask', question: '你说的是哪个 PR？' }, { action: 'duplicate', of: 'task-t9' }];
		const { relay, submitted, notices, logs } = agent(async () => verdicts.shift());
		relay.onUserTurn('现在有几个 pending task？');
		await tick(5);
		relay.onUserTurn('帮我查一下那个 PR');
		await tick(5);
		relay.onUserTurn('再查一下 5333');
		await tick(5);
		assert.deepEqual(submitted, []);
		assert.equal(notices.length, 1);
		assert.match(notices[0], /Ask them now, in their language, exactly: "你说的是哪个 PR？"/);
		assert.ok(logs.some((l) => l.includes('turn not sent, asking the user')));
	});

	it('the turns of one request are checked together once the user has finished speaking', async () => {
		const seen: string[] = [];
		const store = createVoiceTaskStore();
		const speech = createSpeechGate({ quietMs: 60, maxHoldMs: 2_000 });
		const relay = new RelayAgent({ store, speech, classify: async (i) => { seen.push(i.utterance); return { action: 'none' }; }, submit: async () => ({}) });
		speech.onUserSpeechStarted();
		relay.onUserTurn('帮我查一下');
		relay.onUserTurn('5334 的状态');
		await tick(20);
		assert.deepEqual(seen, [], 'still speaking');
		speech.onUserSpeechEnded();
		await tick(120);
		assert.deepEqual(seen, ['帮我查一下 5334 的状态']);
	});

	it('system lines and empty turns are not checked; with no classifier nothing happens', async () => {
		let asked = 0;
		const { relay } = agent(async () => { asked++; return { action: 'none' }; });
		relay.onUserTurn('[System: something]');
		relay.onUserTurn('   ');
		await tick(5);
		assert.equal(asked, 0);
		new RelayAgent({ store: createVoiceTaskStore(), submit: async () => ({}) }).onUserTurn('帮我查一下 5334');
	});

	it('the model\'s reply is read as a decision; anything unclear is none', async () => {
		const { parseTurnVerdict } = await import('../src/work-request-check.js');
		assert.deepEqual(parseTurnVerdict('{"action":"work","task":"查一下 PR 5334"}'), { action: 'work', task: '查一下 PR 5334' });
		assert.deepEqual(parseTurnVerdict('{"action":"work"}'), { action: 'none' }, 'work with no task is not work');
		assert.deepEqual(parseTurnVerdict('{"action":"ask","question":"哪个？"}'), { action: 'ask', question: '哪个？' });
		assert.deepEqual(parseTurnVerdict('ok'), { action: 'none' });
	});

	it('the prompts forbid filling in "that PR" from another request, defer to the assistant\'s own question, and drop fragments', async () => {
		// 2026-10-12 23:53: "帮我查一下那个 PR 的状态" was sent as PR 5302 (from a request 15 minutes earlier) while the
		// voice model was asking "which PR?"; a noise "A" made the user be asked again.
		const { turnClassifierPrompt, workCheckPrompt } = await import('../src/work-request-check.js');
		const turn = turnClassifierPrompt({ utterance: 'A', recentConversation: '' });
		// 2026-10-13 20:54: "Sutando Triage 里其他的 conversation 是啥" was sent to the core although the voice model reads rooms itself.
		const withRooms = turnClassifierPrompt({ utterance: 'A', recentConversation: '' }, ['- none: a room question, the assistant reads the room itself.']);
		assert.ok(withRooms.indexOf('- none: a room question') < withRooms.indexOf('Reply with JSON only'), 'a skill\'s rule sits with the rules');
		assert.doesNotMatch(turn, /room question/);
		assert.match(turn, /"那个 PR".*is never filled in, not even from the request just before it/);
		assert.match(turn, /If it asks the user something about this request .* decide none/);
		assert.match(turn, /A fragment that is not a sentence .* is none/);
		assert.match(workCheckPrompt({ task: "t", recentConversation: "" }), /Do NOT fill it in from a PR, file or meeting that came up earlier, not even in the request just before: ask/);
	});

	it('the checks read the live conversation, so the voice model\'s own question is there at once', async () => {
		const { recentConversationLines } = await import('../src/relay-agent.js');
		const items = [
			{ role: 'user', content: '帮我查一下 5334 的状态。' },
			{ role: 'assistant', content: '[System: something]' },
			{ role: 'tool_call', content: '{}' },
			{ role: 'user', content: '帮我查一下那个餐馆的 menu。' },
			{ role: 'assistant', content: '请问您是指哪一个餐馆呢？' },
		];
		assert.equal(recentConversationLines(items, 2), 'user: 帮我查一下那个餐馆的 menu。\nassistant: 请问您是指哪一个餐馆呢？');
		assert.equal(recentConversationLines(items, 8).split('\n').length, 3, 'system lines and tool items left out');
		const agentSrc = readFileSync(join(import.meta.dirname ?? '.', '..', 'src', 'voice-agent.ts'), 'utf-8');
		assert.match(agentSrc, /recentConversation: \(\) => recentConversationLines\(sessionRef\?\.conversationContext\.items/);
	});

	it('the voice agent hands every real user turn to the relay agent', () => {
		const agentSrc = readFileSync(join(import.meta.dirname ?? '.', '..', 'src', 'voice-agent.ts'), 'utf-8');
		assert.match(agentSrc, /relayAgent\.onUserTurn\(item\.content\)/);
		assert.match(agentSrc, /if \(answeringTools\.has\(e\.toolName\)\) relayAgent\.noteAnsweringTool\(e\.toolName\)/);
		assert.match(agentSrc, /classify: RELAY_CHECK \? createGeminiTurnClassifier\(/);
		assert.match(agentSrc, /extraRules: personalVoiceSurface\.turnCheckRules/, 'the turn check is told what the skills answer themselves');
	});
});

describe('relay agent: a work call waits until the user has finished speaking', () => {
	// Short times stand in for the real 1.5 s quiet and 30 s hold.
	function agent(maxHoldMs = 2_000) {
		const store = createVoiceTaskStore();
		const speech = createSpeechGate({ quietMs: 60, maxHoldMs });
		const submitted: Record<string, unknown>[] = [];
		let n = 0;
		const relay = new RelayAgent({
			store,
			speech,
			submit: async (args) => { submitted.push(args); return { status: 'pending', taskId: `task-s${++n}`, queuedAhead: 0, watcherOnline: true, message: 'm' }; },
		});
		return { relay, speech, submitted };
	}

	it('a request cut off mid-sentence and the rest the user went on to say are submitted as one task', async () => {
		// 2026-10-11 23:31: the model ended the user's turn at "Santander repository 最近一" and called work;
		// the user was still talking and said the whole request again; two tasks were queued.
		const { relay, speech, submitted } = agent();
		speech.onUserSpeechStarted();
		speech.onUserSpeechEnded(); // the provider ends the turn mid-sentence
		const first = relay.invoke('w', { task: 'Check the activity in the Sutando repository for the last week.' });
		await tick(10);
		speech.onUserSpeechStarted(); // 150 ms later the user is still talking
		await tick(100);
		assert.equal(submitted.length, 0, 'nothing is submitted while the user speaks');
		speech.onUserSpeechEnded();
		await tick(20);
		speech.onUserSpeechStarted(); // a short pause between phrases
		await tick(40);
		speech.onUserSpeechEnded();
		await tick(20);
		const second = relay.invoke('w', { task: 'Summarize the PRs merged in the Sutando repository during the last week.' });
		assert.match(await first, /sent together with what they said next\. Say nothing about it/);
		await tick(120);
		assert.equal(submitted.length, 1, 'one task');
		assert.match(String(submitted[0].task), /^Summarize the PRs merged/);
		assert.match(String(submitted[0].task), /cut off mid-sentence: "Check the activity in the Sutando repository/);
		assert.equal(relay.isWaiting('task-s1'), true, 'the later call waits for the result');
		void second;
	});

	it('a request made after the model answered the last one is a new task, whatever the microphone picked up', async () => {
		// 2026-10-11 23:42: four requests in a row, each answered with "Working on it"; between them the
		// microphone picked up sounds too short to be speech (a start with no end). All four were merged.
		const { relay, speech, submitted } = agent();
		for (const task of ['merged PRs this week', 'check PR 5302', 'check PR 5229', 'check PR 5167']) {
			speech.onUserSpeechStarted();
			speech.onUserSpeechEnded();
			void relay.invoke('w', { task });
			await tick(10);
			speech.onModelAudio(); // "Working on it"
			await tick(20);
			speech.onUserSpeechStarted(); // a sound too short to be speech: no end follows
			await tick(10);
		}
		await tick(50);
		assert.deepEqual(submitted.map((a) => a.task), ['merged PRs this week', 'check PR 5302', 'check PR 5229', 'check PR 5167']);
	});

	it('a provider speech start (Gemini: one per transcription chunk, never an end) does not hold a request or merge the next', async () => {
		// 2026-10-12 02:35: "check 5229" was held on a provider start that never ended; "check 5167" five seconds later was merged into it.
		const { relay, speech, submitted } = agent();
		speech.onUserSpeechStarted({ source: 'client-vad' });
		speech.onUserSpeechEnded({ source: 'client-vad' });
		speech.onUserSpeechStarted({ source: 'provider' }); // the transcription chunk arrives after the microphone went quiet
		void relay.invoke('w', { task: 'check PR 5229' });
		await tick(100);
		assert.deepEqual(submitted.map((a) => a.task), ['check PR 5229'], 'submitted once the microphone has been quiet');
		speech.onUserSpeechStarted({ source: 'client-vad' });
		speech.onUserSpeechEnded({ source: 'client-vad' });
		speech.onUserSpeechStarted({ source: 'provider' });
		void relay.invoke('w', { task: 'check PR 5167' });
		await tick(100);
		assert.deepEqual(submitted.map((a) => a.task), ['check PR 5229', 'check PR 5167'], 'two requests, two tasks');
	});

	it('a request the user has finished is submitted once they have been quiet a moment', async () => {
		const { relay, speech, submitted } = agent();
		speech.onUserSpeechStarted();
		speech.onUserSpeechEnded();
		void relay.invoke('w', { task: 'check PR 5302' });
		await tick(20);
		assert.equal(submitted.length, 0, 'not before the quiet time');
		await tick(80);
		assert.deepEqual(submitted.map((a) => a.task), ['check PR 5302']);
	});

	it('two requests with a real pause between them stay two tasks', async () => {
		const { relay, speech, submitted } = agent();
		speech.onUserSpeechStarted();
		speech.onUserSpeechEnded();
		void relay.invoke('w', { task: 'check PR 5302' });
		await tick(100);
		speech.onUserSpeechStarted();
		speech.onUserSpeechEnded();
		void relay.invoke('w', { task: 'check PR 5229' });
		await tick(100);
		assert.deepEqual(submitted.map((a) => a.task), ['check PR 5302', 'check PR 5229']);
	});

	it('a speech start with no end does not hold a request for good', async () => {
		const { relay, speech, submitted } = agent(150);
		speech.onUserSpeechStarted();
		void relay.invoke('w', { task: 'check PR 5302' });
		await tick(60);
		assert.equal(submitted.length, 0);
		await tick(200);
		assert.equal(submitted.length, 1);
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
	it('is one table in memory: what one caller writes, another reads', () => {
		voiceTaskStore.set('task-shared', 'spoken');
		assert.equal(voiceTaskStore.get('task-shared')?.delivery, 'spoken');
	});

	it('never downgrades a spoken result', () => {
		const s = createVoiceTaskStore();
		s.set('task-b', 'spoken');
		s.set('task-b', 'dm');
		assert.equal(s.get('task-b')?.delivery, 'spoken');
	});

	it('calls back after every change, like bodhi\'s history stores; a throwing listener does not stop the table', () => {
		const s = createVoiceTaskStore();
		let calls = 0;
		const off = s.onChange(() => calls++);
		s.onChange(() => { throw new Error('boom'); });
		s.add('task-cb', 'check PR 1');
		s.set('task-cb', 'spoken');
		s.set('task-cb', 'dm'); // a downgrade is not a change
		assert.equal(calls, 2);
		assert.equal(s.get('task-cb')?.delivery, 'spoken');
		off();
		s.noteReplay('task-cb');
		assert.equal(calls, 2, 'unsubscribed');
	});

	it('keeps at most 200 tasks, newest first', () => {
		let t = 0;
		const s = createVoiceTaskStore(() => ++t);
		for (let i = 0; i < 205; i++) s.set(`task-${i}`, 'spoken');
		const ids = s.list().map(([id]) => id);
		assert.equal(ids.length, 200);
		assert.ok(!ids.includes('task-0') && ids.includes('task-204'));
	});

	it('keeps a row per voice task: text, submission, cancel asked, delivery', () => {
		const st = createVoiceTaskStore();
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
		const store = createVoiceTaskStore();
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

	it('the user hears which request a result answers, also when several arrive together', () => {
		assert.ok(frameResult({ text: 'a cat', requests: ['draw a cat'] }).includes(SAY_WHICH_ONE));
		assert.ok(frameResult({ text: 'All three.', requests: ['r1', 'r2', 'r3'] }).includes(SAY_WHICH_EACH));
		assert.ok(!frameResult({ text: 'a cat' }).includes(SAY_WHICH_ONE), 'nothing to name without a request');
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

	it('a result already heard owes nothing', () => {
		const id = finished('check PR 4000', 'PR 4000 is merged.');
		voiceTaskStore.set(id, 'spoken');
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

	it('a task the core answered in another task\'s result owes nothing once that result was heard, also when the link came after', () => {
		const a = `task-${1_800_000_900_000 + ++seq}`;
		const b = `task-${1_800_000_900_000 + ++seq}`;
		writeFileSync(join(TMP, 'tasks', 'archive', MONTH, `${a}.txt`), `id: ${a}\nsource: voice\ntask: check PR 5308\n`);
		writeFileSync(join(TMP, 'tasks', 'archive', MONTH, `${b}.txt`), `id: ${b}\nsource: voice\ntask: check PR 5309\n`);
		writeFileSync(join(TMP, 'results', 'archive', MONTH, `${a}.txt`), 'All three: 5308 merged, 5309 ready.');
		writeFileSync(join(TMP, 'results', 'archive', MONTH, `${b}.txt`), `[deduped: ${a}]`);
		voiceTaskStore.add(a, 'check PR 5308');
		voiceTaskStore.add(b, 'check PR 5309');
		voiceTaskStore.set(a, 'spoken');      // said before the core marked b deduped into it
		voiceTaskStore.setAnsweredBy(b, a);
		assert.equal(voiceTaskStore.get(b)?.delivery, 'spoken', 'b reached the user with a');
		const got: Array<{ text: string; taskId?: string }> = [];
		reconcileVoiceTasks((text, _note, meta) => got.push({ text, taskId: meta.taskId }), () => false);
		assert.equal(got.filter((g) => g.taskId === a || g.taskId === b).length, 0, 'neither is handed over again');
	});

	it('a task answered in another task\'s result that went to the DM is owed that result, labelled for it', () => {
		const a = `task-${1_800_000_900_000 + ++seq}`;
		const b = `task-${1_800_000_900_000 + ++seq}`;
		writeFileSync(join(TMP, 'tasks', 'archive', MONTH, `${a}.txt`), `id: ${a}\nsource: voice\ntask: check PR 5318\n`);
		writeFileSync(join(TMP, 'tasks', 'archive', MONTH, `${b}.txt`), `id: ${b}\nsource: voice\ntask: check PR 5319\n`);
		writeFileSync(join(TMP, 'results', 'archive', MONTH, `${a}.txt`), 'Both: 5318 merged, 5319 ready.');
		writeFileSync(join(TMP, 'results', 'archive', MONTH, `${b}.txt`), `[deduped: ${a}]`);
		voiceTaskStore.add(a, 'check PR 5318');
		voiceTaskStore.add(b, 'check PR 5319');
		voiceTaskStore.setAnsweredBy(b, a);
		voiceTaskStore.set(a, 'dm');
		assert.equal(voiceTaskStore.get(b)?.delivery, 'dm', 'b went where a went');
		const got: Array<{ text: string; taskId?: string }> = [];
		reconcileVoiceTasks((text, _note, meta) => got.push({ text, taskId: meta.taskId }), () => false);
		assert.ok(got.some((g) => g.taskId === b && /5319 ready/.test(g.text)));
		voiceTaskStore.set(a, 'spoken');
		assert.equal(voiceTaskStore.get(b)?.delivery, 'spoken', 'heard with a');
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
		assert.match(runtime, /if \(!opts\.relay\.offerResult\(current\(item\)\)\) return false;/);
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

	it('pending is what the user has not heard an answer to, finished-but-unread results included', async () => {
		// 2026-10-11 00:11:22 UTC: five tasks; two heard, one finished but still waiting to be read, two with
		// the core. The user was told "2 pending"; to them it was 3.
		const { voiceTaskCounts, describeVoiceTasks } = await import('../src/inline-tools.js');
		const row = (text: string, state: string, delivery?: string) => ({ id: text, text, submittedAt: 1, state, delivery }) as never;
		const retired = { id: 'r', text: 'cleared [no-send]', submittedAt: 1, state: 'done', retired: true } as never;
		const rows = [row('merged PRs', 'done', 'spoken'), row('PR 5302', 'done', 'spoken'), row('PR 5167', 'done'), row('PR 5329', 'started'), row('non-spicy dishes', 'queued'), row('old one', 'cancelled'), retired];
		assert.deepEqual(voiceTaskCounts(rows), { pending: 3, working: 2, doneNotHeard: 1 }, 'a task ended with nothing to say is not pending');
		assert.match(describeVoiceTasks(rows), /^Pending for the user: 3 \(they have not heard an answer to these yet: 2 the core is still working on, 1 finished and waiting to be read to them\)\./);
		assert.match(describeVoiceTasks(rows), /1 ended with nothing to say \("cleared \[no-send\]"\)/);
		const tool = src('voice-agent.ts').split("name: 'get_task_status'")[1].split('};')[0];
		assert.ok(tool.includes('voiceTaskCounts(rows)') && tool.includes('pendingTasks: counts.pending') && !tool.includes('openTasks'), 'the tool reports the table\'s count');
	});

	it('the live status snapshot carries every row, newest first, with the same pending count as the tool', async () => {
		const { voiceTaskStatusSnapshot, voiceTaskCounts } = await import('../src/inline-tools.js');
		const row = (text: string, submittedAt: number, state: string, delivery?: string, retired?: boolean) => ({ id: text, text, submittedAt, state, delivery, retired }) as never;
		const rows = [row('PR 5302', 1, 'done', 'spoken'), row('PR 5167', 2, 'done'), row('PR 5329', 3, 'started'), row('cleared', 4, 'done', undefined, true), row('dm one', 5, 'done', 'dm')];
		const snap = voiceTaskStatusSnapshot(rows, 99);
		assert.equal(snap.at, 99);
		assert.deepEqual({ pending: snap.pending, working: snap.working, doneNotHeard: snap.doneNotHeard }, voiceTaskCounts(rows));
		assert.deepEqual(snap.tasks.map((t) => t.id), ['dm one', 'cleared', 'PR 5329', 'PR 5167', 'PR 5302'], 'newest first');
		assert.deepEqual(snap.tasks.map((t) => t.pending), [true, false, true, true, false]);
		assert.deepEqual(snap.tasks.map((t) => t.heard), [false, false, false, false, true]);
		assert.equal(snap.tasks[1].retired, true);
		assert.equal(snap.tasks[0].delivery, 'dm');
		// The voice agent writes it and the web client serves it: one file, one number.
		assert.ok(src('voice-agent.ts').includes("statusPath('voice-task-status.json', WORKSPACE_DIR)"));
		assert.ok(src('web-client.ts').includes("url.pathname === '/voice-tasks'") && src('web-client.ts').includes("'voice-task-status.json'"));
	});

	it('describeVoiceTasks groups the user\'s tasks by where each stands and whether the result was heard', async () => {
		const { describeVoiceTasks } = await import('../src/inline-tools.js');
		const row = (text: string, state: string, delivery?: string) => ({ id: text, text, submittedAt: 1, state, delivery }) as never;
		const out = describeVoiceTasks([row('check PR 5250', 'started'), row('check PR 5259', 'queued'), row('check PR 3509', 'done', 'dm'), row('check PR 5140', 'done', 'spoken')]);
		assert.match(out, /1 the core is working on \("check PR 5250"\)/);
		assert.match(out, /1 queued at the core \("check PR 5259"\)/);
		assert.match(out, /1 done but the user has not heard the result yet \("check PR 3509"\)/);
		assert.match(out, /1 done and already told to the user \("check PR 5140"\)/);
		assert.match(describeVoiceTasks([]), /no tasks/);
	});
});

describe('hand-off pacer: one result at a time, confirmed by the model\'s turn', () => {
	function pacer(confirmMs = 10_000) {
		const spoken: string[][] = [];
		const handed: string[] = [];
		const p = createHandoffPacer({ markSpoken: (ids) => spoken.push(ids), confirmMs });
		const h = (id: string, opts: { hand?: () => boolean; fallback?: () => void } = {}) => ({
			ids: [id], label: id,
			hand: opts.hand ?? (() => { handed.push(id); return true; }),
			fallback: opts.fallback ?? (() => handed.push(`${id} elsewhere`)),
		});
		return { p, h, spoken, handed };
	}

	it('two results landing together: the second waits for the model\'s answer to the first', () => {
		const t = pacer();
		t.p.submit(t.h('a'));
		t.p.submit(t.h('b'));
		assert.deepEqual(t.handed, ['a']);
		t.p.onTurnStart();
		t.p.onModelAudio();
		t.p.onTurnEnd(false);
		assert.deepEqual(t.spoken, [['a']]);
		assert.deepEqual(t.handed, ['a', 'b']);
	});

	it('an answer the model began is said once, also when cut off: recorded spoken, never handed over again', () => {
		// 2026-10-12 01:06: PR 5167's answer was finished, a 213 ms sound counted as a cut-off, and it was read again.
		const t = pacer();
		t.p.submit(t.h('a'));
		t.p.submit(t.h('b'));
		t.p.onTurnStart();
		t.p.onModelAudio();
		t.p.onTurnEnd(true);
		assert.deepEqual(t.spoken, [['a']], 'said once is said');
		assert.deepEqual(t.handed, ['a', 'b'], 'the next result goes; a is not handed over again');
	});

	it('a provider speech start does not hold a result (the pacer listens to the microphone only)', () => {
		const t = pacer();
		t.p.onUserSpeechStarted({ source: 'provider' });
		t.p.submit(t.h('a'));
		assert.deepEqual(t.handed, ['a'], 'not held');
	});

	it('a turn that ended before the hand-off does not confirm it', () => {
		const t = pacer();
		t.p.submit(t.h('a'));
		t.p.onTurnEnd(false);
		assert.deepEqual(t.spoken, []);
		assert.equal(t.p.pending, 1);
	});

	it('no turn at all: the next result goes after the wait', async () => {
		const t = pacer(20);
		t.p.submit(t.h('a'));
		t.p.submit(t.h('b'));
		await tick(40);
		assert.deepEqual(t.handed, ['a', 'b']);
		assert.deepEqual(t.spoken, []);
	});

	it('a result waits while the user speaks and until the model has answered them', () => {
		const t = pacer();
		t.p.onUserSpeechStarted();
		t.p.submit(t.h('a'));
		assert.deepEqual(t.handed, [], 'not while the user is speaking');
		t.p.onUserSpeechEnded();
		assert.deepEqual(t.handed, [], 'not before the model has answered the user');
		t.p.onTurnStart();
		t.p.onModelAudio();
		t.p.onTurnEnd(false);
		assert.deepEqual(t.handed, ['a'], 'after the model answered');
	});

	it('a turn that was only a tool call answered no one: the result still waits, and is not recorded spoken', () => {
		const t = pacer();
		t.p.submit(t.h('a'));
		t.p.onUserSpeechStarted();
		t.p.onUserSpeechEnded();
		t.p.submit(t.h('b'));
		t.p.onTurnStart();
		t.p.onTurnEnd(false); // the model only submitted a work task
		assert.deepEqual(t.spoken, [], 'a silent turn said nothing');
		assert.deepEqual(t.handed, ['a'], 'b still waits for a spoken answer to the user');
		t.p.onTurnStart();
		t.p.onModelAudio();
		t.p.onTurnEnd(false); // the model spoke
		assert.deepEqual(t.spoken, [['a']]);
		assert.deepEqual(t.handed, ['a', 'b']);
	});

	it('the user spoke but no reply came (noise ending a turn): the result goes after the wait', async () => {
		const spoken: string[][] = [];
		const handed: string[] = [];
		const p = createHandoffPacer({ markSpoken: (ids) => spoken.push(ids), userReplyWaitMs: 20 });
		p.onUserSpeechStarted();
		p.onUserSpeechEnded();
		p.submit({ ids: ['a'], label: 'a', hand: () => { handed.push('a'); return true; }, fallback: () => {} });
		assert.deepEqual(handed, []);
		await tick(40);
		assert.deepEqual(handed, ['a']);
	});

	it('the user keeps talking and every reply is cut off: the result goes once the user has been quiet for the wait', async () => {
		const handed: string[] = [];
		const p = createHandoffPacer({ markSpoken: () => {}, userReplyWaitMs: 30 });
		p.onUserSpeechStarted();
		p.submit({ ids: ['a'], label: 'a', hand: () => { handed.push('a'); return true; }, fallback: () => {} });
		p.onUserSpeechEnded();
		await tick(10);
		p.onTurnStart(); p.onModelAudio(); p.onUserSpeechStarted(); p.onTurnEnd(true); // the reply is cut off by more speech
		await tick(40);
		assert.deepEqual(handed, [], 'still speaking: waits');
		p.onUserSpeechEnded();
		await tick(10);
		assert.deepEqual(handed, [], 'just stopped: the wait starts over');
		await tick(40);
		assert.deepEqual(handed, ['a'], 'quiet for the wait, no reply came: it goes');
	});

	it('a speech start whose end never arrives holds results only up to the hand-off wait', async () => {
		const handed: string[] = [];
		const p = createHandoffPacer({ markSpoken: () => {}, confirmMs: 30, userReplyWaitMs: 1000 });
		p.onUserSpeechStarted();
		p.submit({ ids: ['a'], label: 'a', hand: () => { handed.push('a'); return true; }, fallback: () => {} });
		await tick(50);
		assert.deepEqual(handed, ['a']);
	});

	it('the same task is not queued twice; one that cannot be handed goes another way', () => {
		const t = pacer();
		t.p.submit(t.h('a'));
		t.p.submit(t.h('a'));
		t.p.submit(t.h('c', { hand: () => false }));
		t.p.onTurnStart();
		t.p.onModelAudio();
		t.p.onTurnEnd(false);
		assert.deepEqual(t.handed, ['a', 'c elsewhere']);
	});
});

describe('a correction supersedes one request, by a whole mark', () => {
	it('a request is matched only by its sure marks: numbers of three or more digits, quoted names', () => {
		assert.deepEqual(requestMarks('Check the status and details of PR #5302.'), ['5302']);
		assert.deepEqual(requestMarks('查一下 "Meeting notes" 那封邮件'), ['meeting notes']);
		assert.deepEqual(requestMarks('what is the weather tomorrow'), [], 'no sure mark: never matched');
	});

	function agent() {
		const store = createVoiceTaskStore();
		let n = 0;
		const logs: string[] = [];
		const relay = new RelayAgent({ store, log: (m) => logs.push(m), submit: async (args) => { const id = `task-new${++n}`; store.add(id, String(args.task)); return { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true, message: 'm' }; } });
		return { relay, store, logs };
	}

	it('"replaces: pr" supersedes nothing (it was every unheard request)', async () => {
		// Review of #5348, B4: three unheard tasks and replaces:'pr' superseded all three.
		const { relay, store, logs } = agent();
		store.add('task-1', 'Check PR 5302'); store.add('task-2', 'Summarize PR 5251 review'); store.add('task-3', 'Email Bob the PR list');
		void relay.invoke('w', { task: 'Check PR 5320', replaces: 'pr' });
		await tick(5);
		assert.deepEqual(['task-1', 'task-2', 'task-3'].map((id) => store.get(id)?.supersededBy), [undefined, undefined, undefined]);
		assert.ok(logs.some((l) => l.includes('names no task or number')));
	});

	it('5167 is not 51167, and only the most recent request with the mark is superseded', async () => {
		const { relay, store } = agent();
		store.add('task-a', 'Check PR #51167', Date.now() - 2_000);
		store.add('task-b', 'Check PR #5167', Date.now() - 1_500);
		store.add('task-c', 'Summarize PR #51167 again', Date.now() - 1_000);
		void relay.invoke('w', { task: 'Check PR 5167', replaces: '51167' });
		await tick(5);
		assert.equal(store.get('task-a')?.supersededBy, undefined, 'an older one stays');
		assert.equal(store.get('task-b')?.supersededBy, undefined, '5167 is not 51167');
		assert.equal(store.get('task-c')?.supersededBy, 'task-new1');
	});

	it('a task id names exactly that task; a heard request is never superseded', async () => {
		const { relay, store } = agent();
		store.add('task-1001', 'Check PR 7001');
		store.add('task-1002', 'Check PR 7001'); store.set('task-1002', 'spoken');
		void relay.invoke('w', { task: 'Check PR 7002', replaces: 'task-1001' });
		await tick(5);
		assert.equal(store.get('task-1001')?.supersededBy, 'task-new1');
		void relay.invoke('w', { task: 'Check PR 7003', replaces: '7001' });
		await tick(5);
		assert.equal(store.get('task-1002')?.supersededBy, undefined);
	});
});
