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

const { workTool, startResultWatcher, _sweepTimeouts, _pendingTasksForTest } = await import('../src/task-bridge.js');

after(() => rmSync(TMP, { recursive: true, force: true }));

const injected: string[] = [];
startResultWatcher((result) => injected.push(result), () => true);

const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));
const newTaskIds = (before: Set<string>) =>
	[..._pendingTasksForTest.keys()].filter((id) => !before.has(id));
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const call = (task: string, signal: AbortSignal) => (workTool.execute as any)({ task }, { toolCallId: 'call-1', abortSignal: signal }) as Promise<unknown>;

describe('work as a background tool', () => {
	it('is declared background with a pending message that counts the tasks ahead', () => {
		assert.equal(workTool.execution, 'background');
		writeFileSync(join(TMP, 'tasks', 'task-1.txt'), 'task: ahead\n');
		assert.match(workTool.pendingMessage ?? '', /Do NOT tell the user it is done/);
		assert.match(workTool.pendingMessage ?? '', /right after the one I'm on/);
		rmSync(join(TMP, 'tasks', 'task-1.txt'));
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
