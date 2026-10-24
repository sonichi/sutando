// A work call is acknowledged once. nbe2e 2026-10-21 (ws-115544 voice.log): the voice model said "Let me check your
// recent merge history for you." in the turn that called work (turn_8, call 18:56:39.887); the submit's notice asked it
// to say it is on it, and turn_9 added "I'm checking your recent merge activity now." The same for the re-check
// (turn_11 + turn_12) and the video (turn_15 + turn_17). A call whose turn the model spoke in is now told the submit as
// context only; a turn that was only the tool call still gets the one spoken acknowledgement.
// Run: npx tsx --test --test-force-exit tests/relay-agent-work-ack-once.test.ts
import { describe, it, mock } from 'node:test';
import assert from 'node:assert/strict';
import * as relayAgent from '../src/relay-agent.js';
import { RelayAgent, createVoiceTaskStore, createSpeechGate, WORK_ACKNOWLEDGE, DUPLICATE_WAITS, ASK_WAIT_MAX_MS, QUIET_BEFORE_SUBMIT_MS } from '../src/relay-agent.js';
import { parseTurnVerdict, turnClassifierPrompt } from '../src/work-request-check.js';
import type { WorkCheck } from '../src/work-request-check.js';

const settle = async () => { for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r)); };

/** The model's turns as bodhi publishes them (turn.start, response.first_audio, turn.end), as the relay reads them. */
function modelTurns() {
	let open: { spoke: boolean; ended: boolean; whenEnded: Promise<void>; end: () => void } | null = null;
	return {
		watch: {
			onTurnStart() {}, onModelAudio() {}, onTurnEnd() {},
			current: () => open,
		},
		start() { let end!: () => void; const whenEnded = new Promise<void>((r) => { end = r; }); open = { spoke: false, ended: false, whenEnded, end }; },
		audio() { open!.spoke = true; },
		end() { open!.ended = true; open!.end(); open = null; },
	};
}

function relayWith(opts: { check?: () => Promise<WorkCheck>; submitted?: Record<string, unknown>; turns?: ReturnType<typeof modelTurns>; context?: boolean } = {}) {
	const store = createVoiceTaskStore();
	const notices: string[] = [];
	const context: string[] = [];
	let n = 0;
	const turns = opts.turns ?? modelTurns();
	const relay = new RelayAgent({
		store,
		notice: (t) => notices.push(t),
		...(opts.context === false ? {} : { context: (t: string) => context.push(t) }),
		modelTurns: turns.watch,
		check: opts.check,
		submit: async (args) => {
			const id = `task-${++n}`;
			store.add(id, String(args.task));
			return { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true, message: 'm', ...opts.submitted };
		},
	});
	return { relay, store, notices, context, turns };
}
const acks = (notices: string[]) => notices.filter((t) => t.includes(WORK_ACKNOWLEDGE)).length;

describe('a work call the model spoke in is not acknowledged a second time', () => {
	it('turn_8: the model says it is on it while it calls work; the submit is context only', async () => {
		const { relay, notices, context, turns } = relayWith();
		turns.start();
		void relay.invoke('work', { task: 'Count the pull requests I merged yesterday' });
		turns.audio(); // "Let me check your recent merge history for you."
		await settle();
		assert.deepEqual([notices, context], [[], []], 'nothing goes in while the model is still speaking');
		turns.end();
		await settle();
		assert.equal(acks(notices), 0, 'no notice: a notice is answered aloud ("I\'m checking your recent merge activity now.")');
		assert.equal(context.length, 1);
		assert.match(context[0], /The request was sent to the core/);
		assert.match(context[0], /You already told the user you are on it; say nothing more about it until its result comes\. Do not call work again for it\./);
		assert.doesNotMatch(context[0], /Tell the user/);
	});

	it('turn_11: the work check returns after the call\'s turn has ended; the submit is context only, at once', async () => {
		let pass!: (v: WorkCheck) => void;
		const { relay, notices, context, turns } = relayWith({ check: () => new Promise((r) => { pass = r; }) });
		turns.start();
		void relay.invoke('work', { task: 'Check again how many pull requests I merged yesterday' });
		turns.audio(); // "Let me double-check your merge history…"
		turns.end();
		await settle();
		pass({ action: 'submit', lane: 'quick' });
		await settle();
		assert.equal(acks(notices), 0);
		assert.equal(context.length, 1);
	});

	it('turn_15: a turn cut off after the model began speaking still acknowledged it', async () => {
		let pass!: (v: WorkCheck) => void;
		const { relay, notices, context, turns } = relayWith({ check: () => new Promise((r) => { pass = r; }) });
		turns.start();
		void relay.invoke('work', { task: 'Watch the Use Case 03 video and summarize it' });
		turns.audio(); // "I'll access that video room…", interrupted at 18:57:25.139
		turns.end();
		pass({ action: 'submit', lane: 'long' });
		await settle();
		assert.equal(acks(notices), 0);
		assert.equal(context.length, 1);
	});

	it('the decision waits at most ASK_WAIT_MAX_MS for the call\'s turn to end', async () => {
		mock.timers.enable({ apis: ['setTimeout'] });
		try {
			const { relay, notices, context, turns } = relayWith();
			turns.start();
			void relay.invoke('work', { task: 'Count the pull requests I merged yesterday' });
			turns.audio();
			await settle();
			mock.timers.tick(ASK_WAIT_MAX_MS - 1);
			await settle();
			assert.deepEqual([notices, context], [[], []]);
			mock.timers.tick(1);
			await settle();
			assert.equal(acks(notices), 0);
			assert.equal(context.length, 1, 'the model spoke: context only, even though no turn end came');
		} finally {
			mock.timers.reset();
		}
	});

	it('a result that returns before the call\'s turn ends needs no acknowledgement at all', async () => {
		const { relay, notices, context, turns } = relayWith();
		turns.start();
		const call = relay.invoke('work', { task: 'What time is it in Tokyo' });
		turns.audio();
		await settle();
		assert.ok(relay.offerResult({ taskId: 'task-1', text: 'It is 9 pm in Tokyo.' }));
		assert.match(await call, /9 pm in Tokyo/);
		turns.end();
		await settle();
		assert.deepEqual([notices, context], [[], []]);
	});

	it('two calls joined into one request: the earlier call\'s turn spoke, so the later call is context only', async () => {
		mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 0 });
		try {
			const turns = modelTurns();
			const store = createVoiceTaskStore();
			const notices: string[] = [];
			const context: string[] = [];
			const speech = createSpeechGate({ now: () => Date.now() });
			const relay = new RelayAgent({
				store, speech, modelTurns: turns.watch,
				notice: (t) => notices.push(t),
				context: (t) => context.push(t),
				submit: async (args) => { store.add('task-joined', String(args.task)); return { status: 'pending', taskId: 'task-joined', queuedAhead: 0, watcherOnline: true, message: 'm' }; },
			});
			speech.onUserSpeechStarted();
			speech.onUserSpeechEnded();
			turns.start();
			void relay.invoke('work', { task: 'Check the pull requests made by Sonic China' });
			turns.audio(); // "Let me take a look at those pull requests for you."
			turns.end();
			await settle();
			speech.onUserSpeechStarted(); // the user goes on
			mock.timers.tick(1_000);
			speech.onUserSpeechEnded();
			turns.start(); // a turn that is only the later call
			void relay.invoke('work', { task: 'Not Sonic China s o n i c h i' });
			turns.end();
			await settle();
			mock.timers.tick(QUIET_BEFORE_SUBMIT_MS);
			await settle();
			assert.ok(relay.isWaiting('task-joined'));
			assert.equal(acks(notices), 0);
			assert.equal(context.length, 1);
		} finally {
			mock.timers.reset();
		}
	});

	it('a duplicate call made in a turn the model spoke in closes silently when another call already waits', async () => {
		// 18:08:26 (said-once.test.ts): a duplicate found while the first call waits is answered at once by its own call.
		const turns = modelTurns();
		const store = createVoiceTaskStore();
		const seen: string[] = [];
		let checks = 0;
		const one = new RelayAgent({
			store, modelTurns: turns.watch,
			notice: (t) => seen.push(t),
			context: (t) => seen.push(`context: ${t}`),
			check: async () => (++checks === 1 ? { action: 'submit', lane: 'quick' } : { action: 'duplicate', of: 'task-1' }),
			submit: async (args) => { store.add('task-1', String(args.task)); return { status: 'pending', taskId: 'task-1', queuedAhead: 0, watcherOnline: true, message: 'm' }; },
		});
		turns.start();
		void one.invoke('work', { task: 'Check the pull requests made by sonic-china' });
		turns.end();
		await settle();
		assert.equal(acks(seen), 1, 'a silent turn: the one spoken acknowledgement');
		turns.start();
		const again = one.invokeWithScheduling('work', { task: 'Check the pull requests made by sonichi' });
		turns.audio(); // "Got it, I'll update the search for you."
		await settle();
		turns.end();
		const out = await again;
		assert.equal(out.scheduling, 'silent', 'the model already said it is on it: no turn for this answer');
		assert.doesNotMatch(out.text, /Tell the user/);
		assert.equal(seen.filter((t) => t.includes(DUPLICATE_WAITS)).length, 0);
	});

	it('a duplicate call that stays open to wait, in a turn the model spoke in, is context only', async () => {
		const { relay, store, notices, context, turns } = relayWith({ check: async () => ({ action: 'duplicate', of: 'task-open' }) });
		store.add('task-open', 'check PR 5333');
		turns.start();
		void relay.invoke('work', { task: 'what did PR 5333 change' });
		turns.audio();
		turns.end();
		await settle();
		assert.ok(relay.isWaiting('task-open'));
		assert.equal(notices.filter((n) => n.includes(DUPLICATE_WAITS)).length, 0);
		assert.equal(context.length, 1);
		assert.match(context[0], /already sent to the core/);
		assert.match(context[0], /say nothing more about it/);
	});
});

describe('a work call nobody acknowledged yet still is, once', () => {
	it('a turn that was only the tool call: one spoken acknowledgement', async () => {
		const { relay, notices, context, turns } = relayWith();
		turns.start();
		void relay.invoke('work', { task: 'Count the pull requests I merged yesterday' });
		turns.end();
		await settle();
		assert.equal(acks(notices), 1);
		assert.deepEqual(context, []);
	});

	it('a call that comes when no turn is open is acknowledged aloud: an earlier turn\'s speech answered something else', async () => {
		const { relay, notices, context, turns } = relayWith();
		turns.start();
		turns.audio(); // the model spoke, before the user's request
		turns.end();
		void relay.invoke('work', { task: 'Count the pull requests I merged yesterday' });
		await settle();
		assert.equal(acks(notices), 1);
		assert.deepEqual(context, []);
	});

	it('a queue position is news, not an acknowledgement: still said', async () => {
		const { relay, notices, context, turns } = relayWith({ submitted: { queuedAhead: 2, message: 'Queued: 2 tasks are ahead of it.' } });
		turns.start();
		void relay.invoke('work', { task: 'Draw a squirrel' });
		turns.audio();
		turns.end();
		await settle();
		assert.ok(notices.some((t) => t.includes('Queued: 2 tasks are ahead of it.')));
		assert.deepEqual(context, []);
	});

	it('with no context path the acknowledgement is spoken as before', async () => {
		const { relay, notices, turns } = relayWith({ context: false });
		turns.start();
		void relay.invoke('work', { task: 'Count the pull requests I merged yesterday' });
		turns.audio();
		turns.end();
		await settle();
		assert.equal(acks(notices), 1);
	});
});

describe('createModelTurnWatch', () => {
	it('gives the open turn, whether the model spoke in it, and when it ends', async () => {
		const w = relayAgent.createModelTurnWatch();
		assert.equal(w.current(), null);
		w.onTurnStart();
		const mark = w.current()!;
		assert.equal(mark.spoke, false);
		w.onModelAudio();
		assert.equal(mark.spoke, true);
		let ended = false;
		void mark.whenEnded.then(() => { ended = true; });
		w.onTurnEnd();
		await settle();
		assert.ok(ended && mark.ended);
		assert.equal(w.current(), null, 'no turn open after its end');
		w.onModelAudio();
		w.onTurnStart();
		assert.equal(w.current()!.spoke, false, 'a new turn starts silent');
	});
});

// turn_4/turn_5 (18:56:11-22): "Not Sonic, China. S O" was answered "Got it, I'll update the search for you." and sent by
// the turn check as acknowledged. turn_5 "c h i s o n a c h i", answered "Mm-hm.", spelled the same request out; its
// verdict had no acknowledged flag, the notice asked for one, and turn_6 said "I'm currently looking into those pull requests".
describe('a request that corrects one the user already heard acknowledged is not acknowledged again', () => {
	const TASK_4 = 'Check the pull requests made by SO China in the sutando repository about AG2 Sparrow and explain what they do.';
	const TASK_5 = 'Check the pull requests made by chisonachi in the sutando repository about AG2 Sparrow and explain what they do.';

	it('the verdict names the open task it corrects, only one that is open', () => {
		const open = [{ id: 'task-1', text: TASK_4 }];
		assert.deepEqual(parseTurnVerdict(`{"action":"work","task":"${TASK_5}","corrects":"task-1"}`, open), { action: 'work', task: TASK_5, corrects: 'task-1' });
		assert.deepEqual(parseTurnVerdict(`{"action":"work","task":"${TASK_5}","corrects":"task-9"}`, open), { action: 'work', task: TASK_5 });
		assert.deepEqual(parseTurnVerdict(`{"action":"work","task":"${TASK_5}","corrects":7}`, open), { action: 'work', task: TASK_5 });
		const p = turnClassifierPrompt({ utterance: 'c h i s o n a c h i', recentConversation: '', openTasks: open });
		assert.match(p, /- With work, add "corrects":"<id>" when the user is correcting or spelling out one of the open tasks below/);
		assert.ok(p.indexOf('"corrects"') < p.indexOf('Recent words (oldest first):'), 'a rule, not input');
	});

	it('turn_5: the correction of a request acknowledged in turn_4 is context only', async () => {
		const store = createVoiceTaskStore();
		const notices: string[] = [];
		const context: string[] = [];
		const verdicts = [{ action: 'work' as const, task: TASK_4, acknowledged: true as const }, { action: 'work' as const, task: TASK_5, corrects: 'task-1' }];
		let n = 0;
		const relay = new RelayAgent({
			store,
			notice: (t) => notices.push(t),
			context: (t) => context.push(t),
			classify: async () => verdicts.shift()!,
			submit: async (args) => { const id = `task-${++n}`; store.add(id, String(args.task)); return { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true, message: 'm' }; },
		});
		relay.onUserTurn('Not Sonic, China. S O');
		await settle();
		relay.onUserTurn('c h i s o n a c h i');
		await settle();
		assert.equal(store.list().length, 2);
		assert.deepEqual(notices, [], 'no "I\'m currently looking into those pull requests"');
		assert.equal(context.length, 2);
		assert.match(context[1], /chisonachi/);
	});

	it('a correction of a request never acknowledged is acknowledged once, aloud', async () => {
		const store = createVoiceTaskStore();
		store.add('task-0', TASK_4); // sent by something that told the user nothing
		const notices: string[] = [];
		const relay = new RelayAgent({
			store,
			notice: (t) => notices.push(t),
			context: () => {},
			classify: async () => ({ action: 'work', task: TASK_5, corrects: 'task-0' }),
			submit: async (args) => { store.add('task-1', String(args.task)); return { status: 'pending', taskId: 'task-1', queuedAhead: 0, watcherOnline: true, message: 'm' }; },
		});
		relay.onUserTurn('c h i s o n a c h i');
		await settle();
		assert.equal(notices.length, 1);
	});
});
