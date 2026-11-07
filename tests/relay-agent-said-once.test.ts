// One answer is said once. Incidents from the non-blocking test, 2026-10-20 18:05-18:16 UTC (desktop voice-agent.log).
// Run: npx tsx --test --test-force-exit tests/relay-agent-said-once.test.ts
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-said-once-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
const MONTH = new Date().toISOString().slice(0, 7);
for (const d of ['tasks', 'results', join('tasks', 'archive', MONTH), join('results', 'archive', MONTH), join('state', 'activity')]) mkdirSync(join(TMP, d), { recursive: true });

const { RelayAgent, createVoiceTaskStore, DUPLICATE_WAITS, WORK_ACKNOWLEDGE } = await import('../src/relay-agent.js');
const { voiceTaskStore, reconcileVoiceTasks } = await import('../src/task-bridge.js');

after(() => rmSync(TMP, { recursive: true, force: true }));
const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));
// A broken store can leave a call waiting for good: the test fails instead of holding the run open.
const TIMEOUT = 10_000;

describe('a request found already open is acknowledged once', { timeout: TIMEOUT }, () => {
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

describe('two tasks answered by one result: the answer is said once', { timeout: TIMEOUT }, () => {
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
