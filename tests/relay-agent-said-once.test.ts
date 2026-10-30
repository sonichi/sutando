// One answer is said once. Incidents from the non-blocking test, 2026-10-20 18:05-18:16 UTC (desktop voice-agent.log).
// Run: npx tsx --test --test-force-exit tests/relay-agent-said-once.test.ts
import { describe, it, after, mock } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-said-once-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
const MONTH = new Date().toISOString().slice(0, 7);
for (const d of ['tasks', 'results', join('tasks', 'archive', MONTH), join('results', 'archive', MONTH), join('state', 'activity')]) mkdirSync(join(TMP, d), { recursive: true });

const { RelayAgent, createVoiceTaskStore, createSpeechGate, relayAgentSubagentConfig, DUPLICATE_WAITS, WORK_ACKNOWLEDGE, QUIET_BEFORE_SUBMIT_MS } = await import('../src/relay-agent.js');
const { PersistentSubagentManager } = await import('bodhi-realtime-agent');
const { voiceTaskStore, reconcileVoiceTasks } = await import('../src/task-bridge.js');

after(() => rmSync(TMP, { recursive: true, force: true }));
const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe('a request found already open is acknowledged once', () => {
	it('when another call already waits for that task, the call\'s own answer is the only acknowledgement', async () => {
		// 18:08:26 "not Sonic China, S O N I C H I" was found a duplicate of the open task; the model got the
		// "already sent" text twice (a notice and the call's answer) and said "I've updated the request…" twice.
		const store = createVoiceTaskStore();
		const notices: string[] = [];
		let checks = 0;
		const relay = new RelayAgent({
			store,
			notice: (t) => notices.push(t),
			check: async () => (++checks === 1 ? { action: 'submit', lane: 'long' } : { action: 'duplicate', of: 'task-open' }),
			submit: async (args) => { store.add('task-open', String(args.task)); return { status: 'pending', taskId: 'task-open', queuedAhead: 0, watcherOnline: true, message: 'm' }; },
		});
		void relay.invoke('w', { task: 'Check the pull requests made by sonic-china' });
		await tick(5);
		assert.ok(relay.isWaiting('task-open'));
		const again = await relay.invoke('w', { task: 'Check the pull requests made by sonichi' });
		assert.match(again, new RegExp(DUPLICATE_WAITS.slice(0, 40)));
		assert.equal(notices.filter((n) => n.includes(DUPLICATE_WAITS)).length, 0, 'no second copy as a notice');
		assert.equal(notices.filter((n) => n.includes(WORK_ACKNOWLEDGE)).length, 1, 'the first call\'s acknowledgement only');
	});

	it('a duplicate call that stays open to wait is still acknowledged by a notice (the model is silent while it waits)', async () => {
		const store = createVoiceTaskStore();
		store.add('task-open', 'check PR 5333');
		const notices: string[] = [];
		const relay = new RelayAgent({
			store,
			notice: (t) => notices.push(t),
			check: async () => ({ action: 'duplicate', of: 'task-open' }),
			submit: async () => ({ status: 'pending', taskId: 'task-x', queuedAhead: 0, watcherOnline: true, message: 'm' }),
		});
		void relay.invoke('w', { task: 'what did PR 5333 change' });
		await tick(5);
		assert.ok(relay.isWaiting('task-open'));
		assert.equal(notices.filter((n) => n.includes(DUPLICATE_WAITS)).length, 1);
	});
});

describe('two tasks answered by one result: the answer is said once', () => {
	it('heard through either task, the task it shares the answer with is heard too', () => {
		const store = createVoiceTaskStore();
		store.add('task-misheard', 'Check the pull requests made by sonic-china');
		store.add('task-corrected', 'Check the pull requests made by sonichi');
		store.setAnsweredBy('task-misheard', 'task-corrected');
		store.set('task-misheard', 'spoken');
		assert.equal(store.get('task-corrected')?.delivery, 'spoken');
	});

	it('the reconcile pass hands the shared answer over once, not once per task', () => {
		// 18:12:59 both tasks were "result never heard; handing it over again": one went to the misheard task's
		// waiting call (said at 18:13:11), the other into the queue (said again at 18:13:28).
		const misheard = `task-${1_800_000_900_001}`;
		const corrected = `task-${1_800_000_900_002}`;
		const answer = 'AG2 Sparrow is the small piece of software that connects an AI agent on your computer to AG2 Space.';
		for (const [id, text, result] of [[misheard, 'Check the pull requests made by sonic-china', `[deduped: ${corrected}]`], [corrected, 'Check the pull requests made by sonichi', answer]]) {
			writeFileSync(join(TMP, 'tasks', 'archive', MONTH, `${id}.txt`), `id: ${id}\nsource: voice\nchannel_id: local-voice\ntask: ${text}\n`);
			writeFileSync(join(TMP, 'results', 'archive', MONTH, `${id}.txt`), result);
			voiceTaskStore.add(id, text);
		}
		voiceTaskStore.setAnsweredBy(misheard, corrected);
		const owed: string[] = [];
		// As the live delivery does: the task's waiting call takes it and the row is recorded handed over.
		reconcileVoiceTasks((_text, _note, meta) => { owed.push(meta.taskId!); voiceTaskStore.set(meta.taskId!, 'injected'); }, () => false);
		assert.deepEqual(owed.filter((id) => id === misheard || id === corrected).length, 1, `handed once, got ${owed.join(', ')}`);
	});
});

describe('a request joined into the one the user went on to say is acknowledged once', () => {
	it('the joined call closes silently through bodhi\'s dispatch; the later call\'s acknowledgement is the only one', async () => {
		// 2026-10-21 17:12 nbe2e (ws-101157): "...made by Sonic China..." was joined into "Not Sonic China s o n i c h i".
		// The joined call's "Say nothing about it" went back when_idle and the model answered it anyway: turn_5 "I'm
		// looking into those" began at 17:12:26.25, before the later call was submitted (27.389), so before its notice;
		// turn_6 "I will let you know" answered the notice.
		mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 0 });
		try {
			const settle = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); };
			const store = createVoiceTaskStore();
			const notices: string[] = [];
			const submitted: Record<string, unknown>[] = [];
			const speech = createSpeechGate({ now: () => Date.now() });
			const relay = new RelayAgent({
				store,
				speech,
				notice: (t) => notices.push(t),
				check: async () => ({ action: 'submit', lane: 'long' }),
				submit: async (args) => { submitted.push(args); store.add('task-joined', String(args.task)); return { status: 'pending', taskId: 'task-joined', queuedAhead: 0, watcherOnline: true, message: 'm' }; },
			});
			// The session dispatches the work tool through the persistent-subagent manager.
			const mgr = new PersistentSubagentManager();
			const cfg = relayAgentSubagentConfig(relay);
			await mgr.acquirePersistent('relay-agent', cfg, cfg.persistentFactory!);
			speech.onUserSpeechStarted();
			speech.onUserSpeechEnded(); // the provider ends the turn mid-sentence
			const first = mgr.invokeWithScheduling('relay-agent', 'Execute tool: work', { task: 'Check the pull requests made by Sonic China in Sutando repository about AG2 Sparrow' });
			await settle();
			mock.timers.tick(500);
			speech.onUserSpeechStarted(); // the user goes on: the correction
			mock.timers.tick(1_000);
			speech.onUserSpeechEnded();
			const second = mgr.invokeWithScheduling('relay-agent', 'Execute tool: work', { task: 'Not Sonic China s o n i c h i sonichi' });
			await settle();
			mock.timers.tick(QUIET_BEFORE_SUBMIT_MS);
			await settle();
			const closed = await first;
			assert.match(closed.text, /sent together with what they said next/);
			assert.equal(closed.scheduling, 'silent', 'the joined call is closed without a turn of its own');
			assert.equal(submitted.length, 1, 'one task for both');
			assert.match(String(submitted[0].task), /cut off mid-sentence: "Check the pull requests made by Sonic China/);
			assert.ok(relay.isWaiting('task-joined'), 'the later call waits for the result');
			assert.equal(notices.filter((n) => n.includes(WORK_ACKNOWLEDGE)).length, 1, 'its notice is the one acknowledgement');
			void second;
			// Any other answer is delivered as before (when_idle), and invoke() still returns the text.
			const ac = new AbortController();
			ac.abort();
			const notSent = await mgr.invokeWithScheduling('relay-agent', 'Execute tool: work', { task: 'check PR 5333' }, ac.signal);
			assert.equal(notSent.scheduling, undefined);
			assert.equal(typeof await relay.invoke('w', { task: 'check PR 5333' }, ac.signal), 'string');
		} finally {
			mock.timers.reset();
		}
	});
});
