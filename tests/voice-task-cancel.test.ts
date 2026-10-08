// cancel_task decides from where the task really stands (state/activity, results/), never
// from the newest file in tasks/, and writes no "Cancelled." result of its own.
// Run: npx tsx --test --test-force-exit tests/voice-task-cancel.test.ts
import { describe, it, after, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-voice-cancel-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
for (const d of ['tasks', 'results', join('state', 'activity')]) mkdirSync(join(TMP, d), { recursive: true });

const tb = await import('../src/task-bridge.js');
const { cancelTaskTool } = await import('../src/inline-tools.js');
const { _pendingTasksForTest, voiceTaskState, countQueuedAhead, startResultWatcher, setTaskStatusCallback, CANCELLED_BUT_FINISHED_NOTE, _resetVoiceTaskCancelsForTest } = tb;

after(() => rmSync(TMP, { recursive: true, force: true }));

const spoken: Array<{ text: string; note?: string }> = [];
startResultWatcher((text, note) => spoken.push({ text, note }), () => true);
const statuses: Array<{ taskId: string; status: string; text: string }> = [];
setTaskStatusCallback((taskId, status, text) => statuses.push({ taskId, status, text }));

let seq = 0;
function submit(text: string): string {
	const id = `task-${1_800_000_000_000 + ++seq}`;
	writeFileSync(join(TMP, 'tasks', `${id}.txt`), `id: ${id}\nsource: voice\nchannel_id: local-voice\ntask: ${text}\n`);
	_pendingTasksForTest.set(id, { submittedAt: Date.now() + seq, timeoutMs: 3_600_000, dmOnTimeout: false, taskText: text });
	return id;
}
/** The core picked the task up (watcher announce) and, with `engaged`, started working on it. */
function pickUp(id: string, engaged: boolean) {
	writeFileSync(join(TMP, 'state', 'activity', `${id}.json`), JSON.stringify({ task_id: id, phase: 'RUNNING', seq: engaged ? 1 : 0 }));
	rmSync(join(TMP, 'tasks', `${id}.txt`), { force: true });
}
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const cancel = (args: Record<string, unknown> = {}) => (cancelTaskTool.execute as any)(args) as Promise<Record<string, string>>;
const cancelInstructions = () => readdirSync(join(TMP, 'tasks')).filter((f) => f.endsWith('.txt'))
	.filter((f) => readFileSync(join(TMP, 'tasks', f), 'utf-8').includes('CANCEL_INSTRUCTION'));
const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));

beforeEach(() => {
	_pendingTasksForTest.clear();
	_resetVoiceTaskCancelsForTest();
	spoken.length = 0;
	statuses.length = 0;
	for (const f of readdirSync(join(TMP, 'tasks'))) if (f.endsWith('.txt')) rmSync(join(TMP, 'tasks', f));
});

describe('cancel_task decides from the task state', () => {
	it('a queued task is cancelled: instruction written, task file removed, card closed, no "Cancelled." result', async () => {
		const id = submit('draw a car');
		const out = await cancel();
		assert.equal(out.status, 'cancelled');
		assert.equal(out.taskId, id);
		assert.equal(cancelInstructions().length, 1);
		assert.ok(!existsSync(join(TMP, 'tasks', `${id}.txt`)));
		assert.ok(!existsSync(join(TMP, 'results', `${id}.txt`)), 'no stub result');
		assert.ok(statuses.some((s) => s.taskId === id && s.status === 'done' && s.text === 'Cancelled.'));
		assert.ok(!_pendingTasksForTest.has(id), 'out of the timeout sweep');
	});

	it('a task picked up but not yet engaged is still queued', async () => {
		const id = submit('draw a statue');
		pickUp(id, false);
		assert.equal(voiceTaskState(id), 'queued');
		assert.equal((await cancel()).status, 'cancelled');
	});

	it('a task the core is working on is not "cancelled": no files, the user is told it will finish', async () => {
		const id = submit('draw a kiwi');
		pickUp(id, true);
		const out = await cancel();
		assert.equal(out.status, 'already_started');
		assert.match(out.message, /cannot stop partway/);
		assert.deepEqual(cancelInstructions(), []);
		assert.ok(!existsSync(join(TMP, 'results', `${id}.txt`)));
		assert.ok(_pendingTasksForTest.has(id), 'its result is still awaited');
	});

	it('a finished task (result written, not yet read) is left alone: the real result is not overwritten', async () => {
		const id = submit('draw a fence');
		pickUp(id, true);
		writeFileSync(join(TMP, 'results', `${id}.txt`), "Here's your fence.");
		const out = await cancel();
		assert.equal(out.status, 'already_done');
		assert.equal(readFileSync(join(TMP, 'results', `${id}.txt`), 'utf-8'), "Here's your fence.");
		assert.deepEqual(cancelInstructions(), []);
		await tick(2_500);
		assert.deepEqual(spoken.map((s) => s.text), ["Here's your fence."]);
	});

	it('with nothing open from this conversation it cancels nothing, even with other files in tasks/', async () => {
		writeFileSync(join(TMP, 'tasks', 'task-health-1.txt'), 'id: task-health-1\nsource: health\ntask: health check\n');
		const out = await cancel();
		assert.equal(out.status, 'nothing_pending');
		assert.ok(existsSync(join(TMP, 'tasks', 'task-health-1.txt')), 'the health check is untouched');
		assert.deepEqual(cancelInstructions(), []);
	});

	it('targets the latest open task, never a task-health-* that sorts after it', async () => {
		submit('draw a dog');
		const latest = submit('draw a horse');
		writeFileSync(join(TMP, 'tasks', 'task-health-2.txt'), 'id: task-health-2\nsource: health\ntask: health check\n');
		assert.equal((await cancel()).taskId, latest);
		assert.ok(existsSync(join(TMP, 'tasks', 'task-health-2.txt')));
	});

	it('a query matches the open task by its text', async () => {
		const dog = submit('draw a dog driving a car');
		submit('draw a parrot');
		assert.equal((await cancel({ query: 'dog' })).taskId, dog);
	});

	it("the core's reply to the cancel is archived unspoken", async () => {
		submit('draw an apple');
		await cancel();
		const [instruction] = cancelInstructions();
		writeFileSync(join(TMP, 'results', instruction), 'Nothing to cancel — it was never started.');
		await tick(2_500);
		assert.deepEqual(spoken, []);
	});

	it('a cancelled task the core finished anyway is spoken with a note saying so', async () => {
		const id = submit('draw a boat');
		await cancel();
		writeFileSync(join(TMP, 'results', `${id}.txt`), "Here's your boat.");
		await tick(2_500);
		assert.deepEqual(spoken, [{ text: "Here's your boat.", note: CANCELLED_BUT_FINISHED_NOTE }]);
	});
});

describe('queue count', () => {
	it('a task whose result is already in results/ is not counted as ahead', () => {
		const done = submit('draw a cat');
		writeFileSync(join(TMP, 'results', `${done}.txt`), "Here's your cat.");
		const next = submit('explain the weather');
		assert.equal(countQueuedAhead(join(TMP, 'tasks'), next), 0);
		rmSync(join(TMP, 'results', `${done}.txt`));
		assert.equal(countQueuedAhead(join(TMP, 'tasks'), next), 1);
	});
});
