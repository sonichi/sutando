// A task's timeout counts from pickup, not from submission (user feedback
// P1-4): a short-timeout task queued behind other work timed out unpicked, the
// agent said it "ran out of time", and the bridge archived the task file out
// from under the core. The clock now starts when the activity snapshot
// (state/activity/<id>.json) leaves QUEUED; a task still queued past the queue
// bound is reported as unpicked and left in tasks/.
// Run: npx tsx --test --test-force-exit tests/task-bridge-timeout-clock.test.ts
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-timeout-clock-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
mkdirSync(join(TMP, 'tasks'), { recursive: true });
mkdirSync(join(TMP, 'results'), { recursive: true });
mkdirSync(join(TMP, 'state', 'activity'), { recursive: true });

const { _sweepTimeouts, _pendingTasksForTest, _taskActivity, setTaskStatusCallback } = await import('../src/task-bridge.js');

const MIN = 60_000;
const statuses: Array<{ taskId: string; status: string; text: string }> = [];
setTaskStatusCallback((taskId, status, text) => { statuses.push({ taskId, status, text }); });
const spoken: string[] = [];
const onResult = (m: string) => { spoken.push(m); };

function task(id: string, text = 'summarize the thread') {
	writeFileSync(join(TMP, 'tasks', `${id}.txt`), `id: ${id}\nsource: voice\ntask: ${text}\n`);
}
function snapshot(id: string, phase: string) {
	writeFileSync(join(TMP, 'state', 'activity', `${id}.json`), JSON.stringify({ task_id: id, phase }));
}
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

	it('the clock starts when the snapshot leaves QUEUED, and the task times out after its own timeout from there', () => {
		reset();
		task('task-r1');
		const t0 = 1_000_000_000_000;
		_pendingTasksForTest.set('task-r1', { submittedAt: t0, timeoutMs: 5 * MIN, dmOnTimeout: false, taskText: 'x' });
		snapshot('task-r1', 'QUEUED');
		_sweepTimeouts(onResult, t0 + 20 * MIN);
		assert.equal(_pendingTasksForTest.get('task-r1')?.startedAt, undefined, 'QUEUED is not started');
		snapshot('task-r1', 'RUNNING');
		_sweepTimeouts(onResult, t0 + 21 * MIN);
		assert.equal(_pendingTasksForTest.get('task-r1')?.startedAt, t0 + 21 * MIN, 'pickup observed');
		_sweepTimeouts(onResult, t0 + 25 * MIN);
		assert.equal(spoken.length, 0, '4 minutes into a 5-minute task: not timed out (25 minutes since submission)');
		_sweepTimeouts(onResult, t0 + 26 * MIN + 1);
		assert.equal(spoken.length, 1);
		assert.match(spoken[0], /timed out after 5 minutes/);
		assert.equal(statuses[0]?.status, 'timeout');
		assert.ok(!_pendingTasksForTest.has('task-r1'));
		assert.deepEqual(archived(), ['task-r1.txt'], 'a started task that timed out is archived as before');
	});

	it('WAITING (blocked on a person) counts as started; no or unreadable snapshot is "none"', () => {
		reset();
		task('task-w1');
		snapshot('task-w1', 'WAITING');
		assert.equal(_taskActivity('task-w1'), 'started');
		snapshot('task-w1', 'RECEIVED');
		assert.equal(_taskActivity('task-w1'), 'queued');
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
