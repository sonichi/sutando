// `work` is a bodhi background tool: the call returns a pending message at once and
// stays open until its own task's result lands, so the result reaches the model as
// that call's completion (paced and matched to the request) instead of a free-standing
// injection. Without a waiter the old injection path still delivers.
// Run: npx tsx --test --test-force-exit tests/task-bridge-work-background.test.ts
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-work-bg-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
mkdirSync(join(TMP, 'tasks'), { recursive: true });
mkdirSync(join(TMP, 'results'), { recursive: true });

const { workTool, startResultWatcher, _sweepTimeouts, _pendingTasksForTest, setResultPacing, _setHandOffMaxWaitForTest } = await import('../src/task-bridge.js');
const { cancelTaskTool } = await import('../src/inline-tools.js');

after(() => rmSync(TMP, { recursive: true, force: true }));

const injected: string[] = [];
let canTake = true;
startResultWatcher((result) => injected.push(result), () => true, () => canTake);

const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));
const newTaskIds = (before: Set<string>) =>
	[..._pendingTasksForTest.keys()].filter((id) => !before.has(id));
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const call = (task: string, signal: AbortSignal) => (workTool.execute as any)({ task }, { toolCallId: 'call-1', abortSignal: signal }) as Promise<unknown>;

describe('work as a background tool', () => {
	it('is declared background with a fixed pending message (bodhi copies it once per session)', () => {
		assert.equal(workTool.execution, 'background');
		assert.equal(typeof Object.getOwnPropertyDescriptor(workTool, 'pendingMessage')?.value, 'string', 'a value, not a getter');
		assert.match(workTool.pendingMessage ?? '', /Do NOT tell the user it is done/);
		assert.doesNotMatch(workTool.pendingMessage ?? '', /in line|right after/, 'no queue count frozen at session start');
	});

	it('waits for a pause in the conversation before handing the result to its call', async () => {
		let pause!: () => void;
		setResultPacing(() => new Promise<void>((r) => { pause = r; }));
		const before = new Set(_pendingTasksForTest.keys());
		let done: unknown;
		const running = call('draw a cat', new AbortController().signal).then((r) => { done = r; });
		await tick(50);
		const [taskId] = newTaskIds(before);
		writeFileSync(join(TMP, 'results', `${taskId}.txt`), 'Here is your cat.');
		await tick(2_500);
		assert.equal(done, undefined, 'held while the conversation is going');
		pause();
		await running;
		assert.match(String(done), /Here is your cat\./);
		assert.deepEqual(injected, []);
		setResultPacing(async () => {});
	});

	it('resolves the call with its own framed result, without the task id, and injects nothing', async () => {
		const before = new Set(_pendingTasksForTest.keys());
		let done: unknown;
		const running = call('summarize the release notes', new AbortController().signal).then((r) => { done = r; });
		await tick(50);
		const [taskId] = newTaskIds(before);
		assert.ok(taskId, 'a task file was submitted');
		assert.equal(done, undefined, 'the call waits for the result');
		writeFileSync(join(TMP, 'results', `${taskId}.txt`), 'Three fixes and one new flag.');
		await running;
		assert.match(String(done), /TASK_RESULT_START[\s\S]*Three fixes and one new flag\.[\s\S]*TASK_RESULT_END/);
		assert.ok(!String(done).includes(taskId), 'the task id never reaches the model');
		assert.deepEqual(injected, []);
	});

	it('falls back to injection when the call ends before the result', async () => {
		const before = new Set(_pendingTasksForTest.keys());
		const ac = new AbortController();
		const running = call('draft the weekly update', ac.signal);
		await tick(50);
		const [taskId] = newTaskIds(before);
		ac.abort();
		assert.match(String(await running), /delivered separately/);
		writeFileSync(join(TMP, 'results', `${taskId}.txt`), 'Draft ready.');
		await tick(2_500);
		assert.deepEqual(injected, ['Draft ready.']);
		injected.length = 0;
	});

	it('waits out a session that cannot take the result (reconnect) and then hands it to the call', async () => {
		const before = new Set(_pendingTasksForTest.keys());
		let done: unknown;
		const running = call('check the deploy', new AbortController().signal).then((r) => { done = r; });
		await tick(50);
		const [taskId] = newTaskIds(before);
		canTake = false;
		writeFileSync(join(TMP, 'results', `${taskId}.txt`), 'Deploy is green.');
		await tick(3_000);
		assert.equal(done, undefined, 'held while the session reconnects');
		canTake = true;
		await running;
		assert.match(String(done), /Deploy is green\./);
		assert.deepEqual(injected, []);
	});

	it('hands the result back the old way when the session stays unable to take it (DM fallback, meeting hold)', async () => {
		_setHandOffMaxWaitForTest(1_500);
		const before = new Set(_pendingTasksForTest.keys());
		let done: unknown;
		const running = call('check the logs', new AbortController().signal).then((r) => { done = r; });
		await tick(50);
		const [taskId] = newTaskIds(before);
		canTake = false;
		writeFileSync(join(TMP, 'results', `${taskId}.txt`), 'Logs are clean.');
		await running;
		canTake = true;
		_setHandOffMaxWaitForTest(60_000);
		assert.match(String(done), /delivered separately/);
		assert.deepEqual(injected, ['Logs are clean.']);
		injected.length = 0;
	});

	it('a cancelled call ends only at the next pause, after the confirmation turn', async () => {
		const pauses: Array<() => void> = [];
		setResultPacing(() => new Promise<void>((r) => { pauses.push(r); }));
		const before = new Set(_pendingTasksForTest.keys());
		let done: unknown;
		const running = call('draw a horse', new AbortController().signal).then((r) => { done = r; });
		await tick(50);
		const [taskId] = newTaskIds(before);
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const out = await (cancelTaskTool.execute as any)({}) as { taskId?: string; instruction?: string };
		assert.equal(out.taskId, taskId);
		await tick(50);
		assert.equal(done, undefined, 'the call stays open while the model confirms the cancel');
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const again = await (cancelTaskTool.execute as any)({}) as { taskId?: string };
		assert.notEqual(again.taskId, taskId, 'a cancelled call is no longer the target of "cancel it"');
		for (const p of pauses) p();
		await running;
		assert.match(String(done), /cancelled at the user's request/);
		setResultPacing(async () => {});
		for (const id of [taskId, out.instruction!, again.taskId!]) _pendingTasksForTest.delete(id);
	});

	it('a voice cancel ends the waiting call and keeps the stub and the core reply unspoken', async () => {
		const before = new Set(_pendingTasksForTest.keys());
		let done: unknown;
		const running = call('draw a dog', new AbortController().signal).then((r) => { done = r; });
		await tick(50);
		const [taskId] = newTaskIds(before);
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const out = await (cancelTaskTool.execute as any)({}) as { taskId?: string; instruction?: string };
		assert.equal(out.taskId, taskId);
		await running;
		assert.match(String(done), /cancelled at the user's request/);
		writeFileSync(join(TMP, 'results', `${out.instruction}.txt`), 'Nothing to cancel — it was never started.');
		await tick(2_500);
		assert.deepEqual(injected, [], 'neither "Cancelled." nor the core reply is spoken');
		_pendingTasksForTest.delete(taskId);
		_pendingTasksForTest.delete(out.instruction!);
	});

	it('cancel_task with no arguments cancels the latest waiting work call, not the newest file', async () => {
		const before = new Set(_pendingTasksForTest.keys());
		const ac = new AbortController();
		const running = call('draw a mouse', ac.signal);
		await tick(50);
		const [taskId] = newTaskIds(before);
		// Sorted by name, task-health-* comes after task-<digits>; the old default picked it.
		writeFileSync(join(TMP, 'tasks', 'task-health-1.txt'), 'task: health check\n');
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const out = await (cancelTaskTool.execute as any)({}) as { taskId?: string };
		assert.equal(out.taskId, taskId);
		rmSync(join(TMP, 'tasks', 'task-health-1.txt'));
		ac.abort();
		await running;
		_pendingTasksForTest.delete(taskId); // keep it out of the timeout test below
	});

	it('hands a timeout to the waiting call without the task id', async () => {
		const before = new Set(_pendingTasksForTest.keys());
		const running = call('long job', new AbortController().signal);
		await tick(50);
		const [taskId] = newTaskIds(before);
		const pending = _pendingTasksForTest.get(taskId)!;
		pending.startedAt = pending.submittedAt;
		_sweepTimeouts((m) => injected.push(m), pending.submittedAt + pending.timeoutMs + 1);
		const outcome = String(await running);
		assert.match(outcome, /timed out after 60 minutes/);
		assert.ok(!outcome.includes(taskId));
		assert.deepEqual(injected, []);
	});
});
