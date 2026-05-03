// Unit tests for the task-bridge timeout path.
//
// Two layered bugs covered:
//
// (1) Generic timeout title collapsed N distinct timed-out tasks into N
//     visually-identical Tasks-tab rows (16-row screenshot 2026-05-02
//     8.54pm). Fix: timeout status carries the original task text.
//
// (2) Timeout fired `onResult(...)` which was funneled into the voice
//     agent's LLM context, causing it to narrate "the task timed out" out
//     loud — annoying when a slow task often eventually completes anyway.
//     Fix: timeout is UI-only (silent). The badge is set exactly once, the
//     pending entry is left in place so an eventual real result still
//     flips the row to `done` via the normal `_processResult` path.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { unlinkSync } from 'node:fs';
import { join } from 'node:path';
import {
	_checkPendingTimeouts,
	_pendingTasks,
	NUDGE_AT_MS,
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

test('timeout fires UI badge but stays silent (no onResult), pending entry survives', () => {
	const pending = new Map<string, { submittedAt: number; task: string }>();
	pending.set('task-1', { submittedAt: 0, task: 'summarize the 5 open PRs by sonichi' });
	const { statusCalls, onResultCalls, sendStatus, onResult } = makeCaptures();

	_checkPendingTimeouts(TASK_TIMEOUT_MS + 1, pending, sendStatus, onResult, new Set());

	assert.equal(statusCalls.length, 1, 'one timeout status fired');
	assert.equal(statusCalls[0].status, 'timeout');
	assert.equal(onResultCalls.length, 0, 'silent — no voice narration');
	assert.equal(pending.size, 1, 'pending entry kept so an eventual result still flips to done');
});

test('timeout does NOT fire for tasks within the deadline', () => {
	const pending = new Map<string, { submittedAt: number; task: string }>();
	pending.set('task-1', { submittedAt: 0, task: 'still working' });
	const { statusCalls, onResultCalls, sendStatus, onResult } = makeCaptures();

	_checkPendingTimeouts(TASK_TIMEOUT_MS - 1, pending, sendStatus, onResult, new Set());

	assert.equal(statusCalls.length, 0);
	assert.equal(onResultCalls.length, 0);
	assert.equal(pending.size, 1, 'still pending');
});

test('timeout badge dedupes across ticks (does not re-fire every 2s)', () => {
	const pending = new Map<string, { submittedAt: number; task: string }>();
	pending.set('task-1', { submittedAt: 0, task: 'long-running task' });
	const { statusCalls, sendStatus, onResult } = makeCaptures();
	const fired = new Set<string>();

	_checkPendingTimeouts(TASK_TIMEOUT_MS + 1, pending, sendStatus, onResult, fired);
	_checkPendingTimeouts(TASK_TIMEOUT_MS + 2000, pending, sendStatus, onResult, fired);
	_checkPendingTimeouts(TASK_TIMEOUT_MS + 4000, pending, sendStatus, onResult, fired);

	assert.equal(statusCalls.length, 1, 'timeout badge fires exactly once even though the deadline keeps being exceeded');
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
	_checkPendingTimeouts(entry!.submittedAt + TASK_TIMEOUT_MS + 1, _pendingTasks, sendStatus, onResult, new Set());
	const call = statusCalls.find(c => c.taskId === taskId);
	assert.ok(call, 'timeout fired for the submitted task');
	assert.equal(call!.status, 'timeout');
	assert.match(call!.text, /integration-canary task text/);
});

test('nudge fires once at NUDGE_AT_MS, sends a status-check (not a re-submit)', () => {
	const pending = new Map<string, { submittedAt: number; task: string }>();
	pending.set('task-x', { submittedAt: 0, task: 'long-running analysis of the open PRs' });
	const { statusCalls, onResultCalls, sendStatus, onResult } = makeCaptures();
	const fired = new Set<string>();
	const nudge = new Set<string>();
	const nudgeWrites: { id: string; content: string }[] = [];
	const writeNudge = (id: string, content: string) => { nudgeWrites.push({ id, content }); };

	// Below NUDGE_AT_MS — only the regular timeout badge, no nudge yet.
	_checkPendingTimeouts(NUDGE_AT_MS - 1, pending, sendStatus, onResult, fired, nudge, writeNudge);
	assert.equal(nudgeWrites.length, 0, 'no nudge before NUDGE_AT_MS');
	assert.equal(statusCalls.length, 1, 'timeout badge fired (silent)');

	// Past NUDGE_AT_MS — nudge written exactly once.
	_checkPendingTimeouts(NUDGE_AT_MS + 1, pending, sendStatus, onResult, fired, nudge, writeNudge);
	assert.equal(nudgeWrites.length, 1, 'nudge fires once past NUDGE_AT_MS');
	assert.match(nudgeWrites[0].content, /\[Sutando-status-check\]/, 'nudge is a status-check, not a re-submit');
	assert.match(nudgeWrites[0].content, /long-running analysis of the open PRs/, 'nudge cites the original task text so core knows what to report on');
	assert.match(nudgeWrites[0].content, /parent_task_id: task-x/, 'nudge carries parent_task_id for traceability');
	assert.match(nudgeWrites[0].content, /Don't restart the original task/, 'nudge explicitly tells core not to restart');
	assert.equal(onResultCalls.length, 0, 'still silent — nudge does not narrate');

	// Repeat ticks past the nudge threshold — must NOT fire a second nudge.
	_checkPendingTimeouts(NUDGE_AT_MS + 60_000, pending, sendStatus, onResult, fired, nudge, writeNudge);
	_checkPendingTimeouts(NUDGE_AT_MS + 120_000, pending, sendStatus, onResult, fired, nudge, writeNudge);
	assert.equal(nudgeWrites.length, 1, 'nudge dedupes across ticks (only one per pending task)');
});

test('timeout title carries original task text so distinct tasks render distinctly (THIS IS THE BUG)', () => {
	const pending = new Map<string, { submittedAt: number; task: string }>();
	pending.set('task-a', { submittedAt: 0, task: 'summarize the 5 open PRs by sonichi' });
	pending.set('task-b', { submittedAt: 0, task: 'what is on my screen' });
	const { statusCalls, sendStatus, onResult } = makeCaptures();

	_checkPendingTimeouts(TASK_TIMEOUT_MS + 1, pending, sendStatus, onResult, new Set());

	assert.equal(statusCalls.length, 2);
	const titles = statusCalls.map(c => c.text);
	const uniqueTitles = new Set(titles);
	assert.equal(uniqueTitles.size, 2,
		`expected 2 distinct timeout titles for 2 distinct tasks; got ${JSON.stringify(titles)}`);
	assert.match(statusCalls.find(c => c.taskId === 'task-a')!.text, /5 open PRs/);
	assert.match(statusCalls.find(c => c.taskId === 'task-b')!.text, /screen/);
});
