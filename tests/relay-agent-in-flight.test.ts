/**
 * A request the relay agent has accepted but not yet sent is pending (nbe2e 2026-10-21 17:13:24: the user asked
 * "any pending?" while the video request was held for her to finish and then in the work check; the table had no
 * row for it until the submit returned, and get_task_status said "pending 0"). The relay agent keeps the calls in
 * flight; one function turns rows plus in-flight requests into the count and the sentence every reader gives.
 *
 * Run: npx tsx --test --test-force-exit tests/relay-agent-in-flight.test.ts
 */
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-in-flight-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
for (const d of ['state', 'tasks']) mkdirSync(join(TMP, d), { recursive: true });

const { RelayAgent, createVoiceTaskStore } = await import('../src/relay-agent.js');
const inline = await import('../src/inline-tools.js');
const { describeVoiceTasks } = inline;
const { createVoiceTaskStatusWriter } = await import('../src/voice-task-status-writer.js');
type Row = Parameters<typeof describeVoiceTasks>[0][number];
type Sending = { task: string; stage: string };
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const voiceTaskReport = (inline as any).voiceTaskReport as (rows: Row[], sending?: Sending[], readText?: (id: string) => string | null) => { counts: { pending: number; working: number; sending: number; doneNotHeard: number; unknown: number }; description: string };
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const inFlight = (relay: unknown): Sending[] => (relay as any).inFlightRequests();

after(() => rmSync(TMP, { recursive: true, force: true }));

const settle = () => new Promise((r) => setImmediate(r));
const row = (text: string, state: string, delivery?: string) => ({ id: text, text, submittedAt: 1, state, delivery }) as never as Row;

describe('the one report: rows plus requests still being sent', () => {
	const video = { task: 'Watch the use case 03 video and summarize it', stage: 'held' };

	it('17:13:24: no row yet and one request held for the user is one pending, not "no tasks"', () => {
		const out = describeVoiceTasks([], [video] as never);
		assert.doesNotMatch(out, /asked for no tasks/, 'something is being sent');
		assert.match(out, /^Pending: 1\b/);
		assert.match(out, /1 being sent to the core \("Watch the use case 03 video and summ/);
		const r = voiceTaskReport([], [video]);
		assert.deepEqual(r.counts, { pending: 1, working: 0, sending: 1, doneNotHeard: 0, unknown: 0 });
		assert.equal(r.description, out, 'describeVoiceTasks is the report\'s sentence');
	});

	it('a request being sent counts with the ones the core works on; a finished unheard one stays apart', () => {
		const rows = [row('PR 5329', 'started'), row('PR 5167', 'done')];
		const r = voiceTaskReport(rows, [{ task: 'find the apple', stage: 'checking' }], () => 'a result');
		assert.deepEqual(r.counts, { pending: 2, working: 1, sending: 1, doneNotHeard: 1, unknown: 0 });
		assert.match(r.description, /^Pending: 2 \(1 the core is still working on, 1 being sent to the core\); 1 finished and not yet told/);
	});

	it('cancel_task\'s list gives the same number: a request being sent is pending, not "nothing_pending"', async () => {
		inline.setVoiceTasksBeingSent(() => [video]);
		try {
			// eslint-disable-next-line @typescript-eslint/no-explicit-any
			const out = await (inline.cancelTaskTool.execute as any)({ list: true }, null) as { status: string; count: number; tasks: Array<Record<string, unknown>> };
			assert.equal(out.status, 'pending_tasks');
			assert.equal(out.count, 1);
			assert.deepEqual(out.tasks, [{ preview: 'Watch the use case 03 video and summarize it', state: 'being sent' }]);
		} finally {
			inline.setVoiceTasksBeingSent(() => []);
		}
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		assert.equal((await (inline.cancelTaskTool.execute as any)({ list: true }, null)).status, 'nothing_pending');
	});

	it('with nothing being sent the sentence is exactly what it was', () => {
		const rows = [row('PR 5329', 'started'), row('non-spicy dishes', 'queued'), row('PR 5167', 'done')];
		assert.equal(voiceTaskReport(rows).description, describeVoiceTasks(rows));
		assert.match(voiceTaskReport(rows).description, /^Pending: 2 the core is still working on; 1 finished and not yet told to the user/);
		assert.equal(voiceTaskReport([]).description, 'The user has asked for no tasks by voice in the last few hours.');
	});
});

describe('the relay agent lists each accepted call until its submit returns', () => {
	function setup(check: () => Promise<{ action: 'submit' } | { action: 'ask'; question: string }> = async () => ({ action: 'submit' }), beforeRow: () => Promise<void> = async () => {}) {
		const store = createVoiceTaskStore();
		let release!: () => void;
		const turnOver = new Promise<void>((r) => (release = r));
		let checkRelease!: () => void;
		const checkGate = new Promise<void>((r) => (checkRelease = r));
		const atSubmit: Sending[][] = [];
		let changes = 0;
		const relay = new RelayAgent({
			store,
			speech: { waitTurnOver: () => turnOver } as never,
			check: async () => { await checkGate; return check(); },
			submit: async (args) => {
				atSubmit.push(inFlight(relay));
				await beforeRow(); // the submit's own waits (spoken turns, the core's queue) before the row is added
				const id = `task-${store.list().length + 1}`;
				store.add(id, String(args.task));
				return { status: 'pending', taskId: id };
			},
			onInFlightChange: () => { changes++; },
		} as never);
		const rows = (): Row[] => store.list().map(([id, r]) => ({ id, text: r.text ?? '', submittedAt: r.submittedAt ?? 0, state: 'queued' }) as Row);
		return { relay, store, release, checkRelease, atSubmit, rows, changes: () => changes };
	}

	it('held, then in the check, then being submitted, then gone the moment the row exists', async () => {
		const s = setup();
		void s.relay.invoke('work', { task: 'summarize the use case 03 video' });
		await settle();
		assert.deepEqual(inFlight(s.relay), [{ task: 'summarize the use case 03 video', stage: 'held' }], 'held for the user to finish: listed');
		assert.equal(voiceTaskReport(s.rows(), inFlight(s.relay)).counts.pending, 1);
		s.release();
		await settle();
		assert.deepEqual(inFlight(s.relay).map((e) => e.stage), ['checking']);
		s.checkRelease();
		await settle();
		assert.deepEqual(s.atSubmit, [[{ task: 'summarize the use case 03 video', stage: 'sending' }]], 'still listed while the submit runs');
		assert.deepEqual(inFlight(s.relay), [], 'gone once the submit returned');
		assert.equal(s.store.list().length, 1);
		assert.equal(voiceTaskReport(s.rows(), inFlight(s.relay)).counts.pending, 1, 'the row, not the row and the call');
		assert.ok(s.changes() >= 2, 'each entry and exit is announced');
	});

	it('a call joined into a held one is one request, under the joined text', async () => {
		const s = setup();
		void s.relay.invoke('work', { task: 'watch the video' });
		await settle();
		void s.relay.invoke('work', { task: 'and summarize it' });
		await settle();
		const list = inFlight(s.relay);
		assert.equal(list.length, 1, 'counted once from the moment they join');
		assert.ok(list[0].task.includes('and summarize it') && list[0].task.includes('watch the video'), `the joined text carries both: ${list[0].task}`);
		s.release();
		s.checkRelease();
		await settle();
		assert.deepEqual(inFlight(s.relay), []);
		assert.equal(voiceTaskReport(s.rows(), inFlight(s.relay)).counts.pending, 1);
	});

	it('a call that ends without a submit (the user is asked) leaves the list', async () => {
		const s = setup(async () => ({ action: 'ask', question: 'which video?' }));
		const call = s.relay.invoke('work', { task: 'that video' });
		s.release();
		s.checkRelease();
		await call;
		assert.deepEqual(inFlight(s.relay), []);
		assert.equal(s.store.list().length, 0);
	});

	it('a turn the turn check sends is listed while its submit runs', async () => {
		const store = createVoiceTaskStore();
		const atSubmit: Sending[][] = [];
		const relay = new RelayAgent({
			store,
			classify: async () => ({ action: 'work', task: 'check PR 5790' }),
			submit: async (args) => {
				atSubmit.push(inFlight(relay));
				store.add('task-1', String(args.task));
				return { status: 'pending', taskId: 'task-1' };
			},
		} as never);
		relay.onUserTurn('check PR 5790');
		await settle();
		await settle();
		assert.deepEqual(atSubmit, [[{ task: 'check PR 5790', stage: 'sending' }]]);
		assert.deepEqual(inFlight(relay), []);
	});

	it('the /voice-tasks copy never counts a request twice while its row is being added', async () => {
		// The submit takes long enough that no copy is being written when the row is added (live: spoken turns, the core).
		const s = setup(undefined, async () => { await settle(); await w.flush(); });
		const counts: number[] = [];
		const w = createVoiceTaskStatusWriter({
			path: join(TMP, 'state', 'voice-task-status.json'),
			snapshot: () => { const c = voiceTaskReport(s.rows(), inFlight(s.relay)).counts.pending; counts.push(c); return { pending: c }; },
		});
		s.store.onChange(() => w.changed());
		// The relay agent's own announcements, as voice-agent wires them.
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		(s.relay as any).deps.onInFlightChange = () => w.changed();
		void s.relay.invoke('work', { task: 'summarize the video' });
		await settle();
		await w.flush();
		s.release();
		s.checkRelease();
		for (let i = 0; i < 50 && (s.store.list().length === 0 || inFlight(s.relay).length); i++) await settle();
		await settle();
		await w.flush();
		assert.equal(s.store.list().length, 1);
		assert.ok(counts.length >= 2, `snapshots: ${counts}`);
		assert.deepEqual([...new Set(counts)], [1], `every snapshot says 1 (the call, then its row): ${counts}`);
	});
});
