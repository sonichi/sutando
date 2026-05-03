// Unit tests for the task-bridge timeout path.
//
// Bug being covered: when a task hits TASK_TIMEOUT_MS, the status event
// fired to the web UI used a generic title — "Task timed out — core agent
// may be unresponsive" — which collapses N distinct timed-out tasks into
// N visually-identical Tasks-tab rows (see screenshot 2026-05-02 8.54pm:
// 16 indistinguishable rows). The desired behavior is that the timeout
// status carries the original task text so each row remains identifiable.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { unlinkSync } from 'node:fs';
import { join } from 'node:path';
import {
	_checkPendingTimeouts,
	_pendingTasks,
	TASK_TIMEOUT_MS,
	workTool,
} from '../src/task-bridge.ts';

const REPO_DIR = new URL('..', import.meta.url).pathname.replace(/\/$/, '');
const TASK_DIR = join(REPO_DIR, 'tasks');

type StatusCall = { taskId: string; status: string; text: string; result?: string };

function makeCaptures() {
	const statusCalls: StatusCall[] = [];
	const onResultCalls: string[] = [];
	const sendStatus = (taskId: string, status: string, text: string, result?: string) => {
		statusCalls.push({ taskId, status, text, result });
	};
	const onResult = (s: string) => { onResultCalls.push(s); };
	return { statusCalls, onResultCalls, sendStatus, onResult };
}

test('timeout fires for tasks older than TASK_TIMEOUT_MS', () => {
	const pending = new Map<string, { submittedAt: number; task: string }>();
	pending.set('task-1', { submittedAt: 0, task: 'summarize the 5 open PRs by sonichi' });
	const { statusCalls, onResultCalls, sendStatus, onResult } = makeCaptures();

	_checkPendingTimeouts(TASK_TIMEOUT_MS + 1, pending, sendStatus, onResult);

	assert.equal(statusCalls.length, 1, 'one timeout status fired');
	assert.equal(statusCalls[0].status, 'timeout');
	assert.equal(onResultCalls.length, 1);
	assert.equal(pending.size, 0, 'pending entry removed after timeout');
});

test('timeout does NOT fire for tasks within the deadline', () => {
	const pending = new Map<string, { submittedAt: number; task: string }>();
	pending.set('task-1', { submittedAt: 0, task: 'still working' });
	const { statusCalls, onResultCalls, sendStatus, onResult } = makeCaptures();

	_checkPendingTimeouts(TASK_TIMEOUT_MS - 1, pending, sendStatus, onResult);

	assert.equal(statusCalls.length, 0);
	assert.equal(onResultCalls.length, 0);
	assert.equal(pending.size, 1, 'still pending');
});

test('integration: workTool submission stores task text so timeout title remains identifiable', async () => {
	// Guards against a regression where the submit-site stops recording the
	// task text into _pendingTasks (e.g. someone reverts the value shape from
	// {submittedAt, task} to a bare timestamp). Without the task text in the
	// map, the timeout title would collapse back to "Timed out: undefined"
	// for every entry — same UI symptom as the original bug.
	_pendingTasks.clear();
	const taskText = 'integration-canary task text — should reach timeout title';
	const result = await workTool.execute({ task: taskText }, null as any);
	assert.equal((result as any).status, 'pending', 'workTool returns pending');
	const taskId = (result as any).taskId as string;
	// workTool writes tasks/<taskId>.txt on disk — remove it so the live
	// watcher (if running) doesn't fire a TASK_FILE event for our fixture.
	try { unlinkSync(join(TASK_DIR, `${taskId}.txt`)); } catch {}
	const entry = _pendingTasks.get(taskId);
	assert.ok(entry, 'task is registered in _pendingTasks');
	assert.equal(entry!.task, taskText, 'submit-site preserves the original task text');
	assert.equal(typeof entry!.submittedAt, 'number');

	// Drive the timeout and confirm the title carries the text end-to-end.
	const { statusCalls, sendStatus, onResult } = makeCaptures();
	_checkPendingTimeouts(entry!.submittedAt + TASK_TIMEOUT_MS + 1, _pendingTasks, sendStatus, onResult);
	const call = statusCalls.find(c => c.taskId === taskId);
	assert.ok(call, 'timeout fired for the submitted task');
	assert.equal(call!.status, 'timeout');
	assert.match(call!.text, /integration-canary task text/);
});

test('timeout title carries original task text so distinct tasks render distinctly (THIS IS THE BUG)', () => {
	const pending = new Map<string, { submittedAt: number; task: string }>();
	pending.set('task-a', { submittedAt: 0, task: 'summarize the 5 open PRs by sonichi' });
	pending.set('task-b', { submittedAt: 0, task: 'what is on my screen' });
	const { statusCalls, sendStatus, onResult } = makeCaptures();

	_checkPendingTimeouts(TASK_TIMEOUT_MS + 1, pending, sendStatus, onResult);

	assert.equal(statusCalls.length, 2);
	const titles = statusCalls.map(c => c.text);
	const uniqueTitles = new Set(titles);
	assert.equal(uniqueTitles.size, 2,
		`expected 2 distinct timeout titles for 2 distinct tasks; got ${JSON.stringify(titles)}`);
	assert.match(statusCalls.find(c => c.taskId === 'task-a')!.text, /5 open PRs/);
	assert.match(statusCalls.find(c => c.taskId === 'task-b')!.text, /screen/);
});
