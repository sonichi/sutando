// A task's timeout counts from pickup, not from submission (user feedback
// P1-4): a short-timeout task queued behind other work timed out unpicked, the
// agent said it "ran out of time", and the bridge archived the task file out
// from under the core. The clock now starts when the activity snapshot
// (state/activity/<id>.json) leaves QUEUED; a task still queued past the queue
// bound is reported as unpicked and left in tasks/.
// Run: npx tsx --test --test-force-exit tests/task-bridge-timeout-clock.test.ts
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-timeout-clock-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
mkdirSync(join(TMP, 'tasks'), { recursive: true });
mkdirSync(join(TMP, 'results'), { recursive: true });
mkdirSync(join(TMP, 'state', 'activity'), { recursive: true });

const { _sweepTimeouts, _pendingTasksForTest, _taskActivity, _taskHasWorkingRow, setTaskStatusCallback } = await import('../src/task-bridge.js');
import { spawnSync } from 'node:child_process';
const REPO = new URL('..', import.meta.url).pathname;

const MIN = 60_000;
const statuses: Array<{ taskId: string; status: string; text: string }> = [];
setTaskStatusCallback((taskId, status, text) => { statuses.push({ taskId, status, text }); });
const spoken: string[] = [];
const onResult = (m: string) => { spoken.push(m); };

function task(id: string, text = 'summarize the thread') {
	writeFileSync(join(TMP, 'tasks', `${id}.txt`), `id: ${id}\nsource: voice\ntask: ${text}\n`);
}
function snapshot(id: string, phase: string, extra: Record<string, unknown> = {}) {
	writeFileSync(join(TMP, 'state', 'activity', `${id}.json`), JSON.stringify({ task_id: id, phase, ...extra }));
}
function workingRow(id: string, kind = 'working', projection?: string) {
	const rec: Record<string, unknown> = { ts: Date.now() / 1000, line: 'x', kind, task: { id } };
	if (projection) rec.projection = projection;
	writeFileSync(join(TMP, 'state', 'agent-activity.jsonl'), JSON.stringify(rec) + '\n', { flag: 'a' });
}
/** The watcher's own dispatch: task-emit.sh's emit_dispatch_task_file, which marks RUNNING at announce. */
function dispatchLikeTheWatcher(id: string) {
	const r = spawnSync('bash', ['-c', `source "$1"; emit_dispatch_task_file ${id}.txt`, '_', join(REPO, 'src', 'task-emit.sh')], {
		env: { ...process.env, TASKS_DIR: join(TMP, 'tasks'), SUTANDO_TEST_MODE: '1', SUTANDO_WORKSPACE: TMP, SUTANDO_PY_BIN: 'python3' },
		encoding: 'utf-8',
	});
	assert.equal(r.status, 0, r.stderr);
	assert.match(r.stdout, new RegExp(`^TASK_FILE: ${id}\\.txt`));
}
const until = async (cond: () => boolean, ms: number) => { const t0 = Date.now(); while (!cond() && Date.now() - t0 < ms) await new Promise((r) => setTimeout(r, 100)); return cond(); };
const archived = () => {
	const root = join(TMP, 'tasks', 'archive');
	if (!existsSync(root)) return [];
	return readdirSync(root).flatMap((m) => readdirSync(join(root, m)));
};
function reset() { _pendingTasksForTest.clear(); statuses.length = 0; spoken.length = 0; }

after(() => rmSync(TMP, { recursive: true, force: true }));

describe('the task timeout counts from pickup', () => {
	it('a queued task past its own timeout is neither timed out nor archived', () => {
		reset();
		task('task-q1');
		snapshot('task-q1', 'QUEUED');
		const t0 = 1_000_000_000_000;
		_pendingTasksForTest.set('task-q1', { submittedAt: t0, timeoutMs: 5 * MIN, dmOnTimeout: false, taskText: 'x' });
		_sweepTimeouts(onResult, t0 + 30 * MIN);
		assert.equal(spoken.length, 0, 'nothing spoken while the task is still queued');
		assert.equal(statuses.length, 0);
		assert.ok(_pendingTasksForTest.has('task-q1'), 'still pending');
		assert.ok(existsSync(join(TMP, 'tasks', 'task-q1.txt')), 'the task file stays for the core');
		assert.deepEqual(archived(), []);
	});

	it('the clock starts on ENGAGEMENT, not on the RUNNING the watcher writes at announce', () => {
		reset();
		task('task-r1');
		const t0 = 1_000_000_000_000;
		_pendingTasksForTest.set('task-r1', { submittedAt: t0, timeoutMs: 5 * MIN, dmOnTimeout: false, taskText: 'x' });
		snapshot('task-r1', 'QUEUED');
		_sweepTimeouts(onResult, t0 + 20 * MIN);
		assert.equal(_pendingTasksForTest.get('task-r1')?.startedAt, undefined, 'QUEUED is not started');
		snapshot('task-r1', 'RUNNING');   // announced: delivered, not worked
		_sweepTimeouts(onResult, t0 + 20 * MIN + 30_000);
		assert.equal(_pendingTasksForTest.get('task-r1')?.startedAt, undefined, 'RUNNING at announce is delivery, not engagement');
		assert.equal(spoken.length, 0);
		workingRow('task-r1', 'processing');           // the core read it: still not work
		workingRow('task-r1', 'working', 'TASK_STATUS'); // the bus's own delivery row: not work
		_sweepTimeouts(onResult, t0 + 20 * MIN + 40_000);
		assert.equal(_pendingTasksForTest.get('task-r1')?.startedAt, undefined);
		workingRow('task-r1');                         // a tool call on the task
		_sweepTimeouts(onResult, t0 + 21 * MIN);
		assert.equal(_pendingTasksForTest.get('task-r1')?.startedAt, t0 + 21 * MIN, 'engagement observed');
		_sweepTimeouts(onResult, t0 + 25 * MIN);
		assert.equal(spoken.length, 0, '4 minutes into a 5-minute task: not timed out (25 minutes since submission)');
		_sweepTimeouts(onResult, t0 + 26 * MIN + 1);
		assert.equal(spoken.length, 1);
		assert.match(spoken[0], /timed out after 5 minutes/);
		assert.equal(statuses[0]?.status, 'timeout');
		assert.ok(!_pendingTasksForTest.has('task-r1'));
		assert.deepEqual(archived(), ['task-r1.txt'], 'a started task that timed out is archived as before');
	});

	it('a runtime event on the snapshot is engagement; WAITING without one is not; no snapshot is "none"', () => {
		reset();
		task('task-w1');
		snapshot('task-w1', 'WAITING', { seq: 3 });
		assert.equal(_taskActivity('task-w1'), 'started');
		snapshot('task-w1', 'WAITING');
		assert.equal(_taskActivity('task-w1'), 'queued', 'a phase alone is not engagement');
		snapshot('task-w1', 'RUNNING', { seq: 0 });
		assert.equal(_taskActivity('task-w1'), 'queued');
		snapshot('task-w1', 'COMPLETED');
		assert.equal(_taskActivity('task-w1'), 'started', 'a terminal task is past pickup');
		snapshot('task-w1', 'RECEIVED');
		assert.equal(_taskActivity('task-w1'), 'queued');
		assert.equal(_taskHasWorkingRow('task-w1'), false);
		assert.equal(_taskActivity('task-none'), 'none', 'no snapshot: the pickup is invisible');
		writeFileSync(join(TMP, 'state', 'activity', 'task-bad.json'), '{not json');
		assert.equal(_taskActivity('task-bad'), 'none', 'unreadable: invisible');
		writeFileSync(join(TMP, 'state', 'activity', 'task-odd.json'), JSON.stringify({ phase: 7 }));
		assert.equal(_taskActivity('task-odd'), 'none');
	});

	it('a runtime that never writes a snapshot keeps the submission clock (review of #4864)', () => {
		// The Windows dispatcher and other runtimes emit no activity; a picked-up
		// task that hangs there must still time out on its own timeout, archived
		// as before, never wait the queue bound and read as "never picked up".
		reset();
		task('task-n1', 'no activity here');
		const t0 = 1_000_000_000_000;
		_pendingTasksForTest.set('task-n1', { submittedAt: t0, timeoutMs: 5 * MIN, dmOnTimeout: false, taskText: 'x' });
		_sweepTimeouts(onResult, t0 + 4 * MIN);
		assert.equal(spoken.length, 0);
		_sweepTimeouts(onResult, t0 + 5 * MIN + 1);
		assert.equal(spoken.length, 1);
		assert.match(spoken[0], /timed out after 5 minutes/);
		assert.doesNotMatch(spoken[0], /has not been picked up/);
		assert.ok(!_pendingTasksForTest.has('task-n1'));
		assert.ok(archived().includes('task-n1.txt'), 'archived as before');
	});

	it('a task nobody picks up within the queue bound is reported as unpicked and left in tasks/', () => {
		reset();
		task('task-u1', 'draft the reply');
		snapshot('task-u1', 'QUEUED');
		const t0 = 1_000_000_000_000;
		_pendingTasksForTest.set('task-u1', { submittedAt: t0, timeoutMs: 5 * MIN, dmOnTimeout: false, taskText: 'x' });
		_sweepTimeouts(onResult, t0 + 59 * MIN);
		assert.equal(spoken.length, 0, 'inside the 60-minute queue bound');
		_sweepTimeouts(onResult, t0 + 61 * MIN);
		assert.equal(spoken.length, 1);
		assert.match(spoken[0], /has not been picked up after 61 minutes\. It is still queued/);
		assert.match(spoken[0], /draft the reply/);
		assert.equal(statuses[0]?.status, 'timeout');
		assert.ok(!_pendingTasksForTest.has('task-u1'));
		assert.ok(existsSync(join(TMP, 'tasks', 'task-u1.txt')), 'never archived: the core still finds it');
		assert.ok(!archived().includes('task-u1.txt'));
	});

	it('a longer per-task timeout also stretches the queue bound', () => {
		reset();
		task('task-l1');
		snapshot('task-l1', 'QUEUED');
		const t0 = 1_000_000_000_000;
		_pendingTasksForTest.set('task-l1', { submittedAt: t0, timeoutMs: 120 * MIN, dmOnTimeout: false, taskText: 'x' });
		_sweepTimeouts(onResult, t0 + 90 * MIN);
		assert.equal(spoken.length, 0);
		_sweepTimeouts(onResult, t0 + 121 * MIN);
		assert.equal(spoken.length, 1);
	});

	it('driven by the watcher itself: a 5-minute task queued behind a long one is not timed out or archived', async () => {
		// Review of #4864 (Rui): task-emit.sh marks RUNNING the moment it announces a
		// task, so the second of two tasks is RUNNING while the core is still on the
		// first. Its own timeout must not run until the core engages with it.
		reset();
		task('task-long', 'a long job');
		task('task-short', 'quick question');
		dispatchLikeTheWatcher('task-long');
		dispatchLikeTheWatcher('task-short');
		const snap = join(TMP, 'state', 'activity', 'task-short.json');
		assert.ok(await until(() => existsSync(snap), 5000), 'the watcher wrote the activity snapshot');
		assert.equal(JSON.parse(readFileSync(snap, 'utf-8')).phase, 'RUNNING', 'RUNNING at announce, as the watcher does');
		assert.equal(_taskActivity('task-short'), 'queued', 'announced but not engaged');
		const t0 = Date.now();
		_pendingTasksForTest.set('task-short', { submittedAt: t0, timeoutMs: 5 * MIN, dmOnTimeout: false, taskText: 'quick question' });
		_sweepTimeouts(onResult, t0);           // the sweep that saw the announce
		_sweepTimeouts(onResult, t0 + 6 * MIN);
		assert.equal(spoken.length, 0, 'no timeout while the core is still on the long task');
		assert.ok(existsSync(join(TMP, 'tasks', 'task-short.txt')), 'the task file stays for the core');
		assert.ok(!archived().includes('task-short.txt'));
		workingRow('task-short');  // the core's activity hook attributes a tool call to it
		_sweepTimeouts(onResult, t0 + 7 * MIN);
		assert.equal(_pendingTasksForTest.get('task-short')?.startedAt, t0 + 7 * MIN, 'the clock starts at engagement');
		_sweepTimeouts(onResult, t0 + 12 * MIN + 1);
		assert.equal(spoken.length, 1);
		assert.match(spoken[0], /timed out after 5 minutes/);
	});

	it('timeout 0 means no timeout, queued or running', () => {
		reset();
		task('task-z1');
		const t0 = 1_000_000_000_000;
		_pendingTasksForTest.set('task-z1', { submittedAt: t0, timeoutMs: 0, dmOnTimeout: false, taskText: 'x' });
		snapshot('task-z1', 'RUNNING');
		_sweepTimeouts(onResult, t0 + 10_000 * MIN);
		assert.equal(spoken.length, 0);
		assert.ok(_pendingTasksForTest.has('task-z1'));
	});
});
