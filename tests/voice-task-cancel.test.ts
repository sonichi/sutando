// cancel_task decides from where the task really stands (state/activity, results/), never
// from the newest file in tasks/, and writes no "Cancelled." result of its own.
// Run: npx tsx --test --test-force-exit tests/voice-task-cancel.test.ts
import { describe, it, after, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { spawnSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-voice-cancel-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
for (const d of ['tasks', 'results', join('state', 'activity')]) mkdirSync(join(TMP, d), { recursive: true });

const tb = await import('../src/task-bridge.js');
const { cancelTaskTool } = await import('../src/inline-tools.js');
const { _pendingTasksForTest, voiceTaskState, startResultWatcher, setTaskStatusCallback, CANCELLED_BUT_FINISHED_NOTE, _resetVoiceTaskCancelsForTest } = tb;

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
const HOOK = join(dirname(fileURLToPath(import.meta.url)), '..', 'skills', 'agent-activity', 'hooks', 'activity-hook.py');
/** One tool call by the core session, through the real activity hook. */
function coreTool(tool: string, input: Record<string, unknown>) {
	const payload = JSON.stringify({ hook_event_name: 'PreToolUse', session_id: 'core', tool_name: tool, tool_input: input });
	const r = spawnSync('python3', [HOOK], { input: payload, env: { ...process.env, SUTANDO_TEST_MODE: '1', SUTANDO_WORKSPACE: TMP }, encoding: 'utf-8' });
	assert.equal(r.status, 0, r.stderr);
}
/** The watcher announced the task (RUNNING, seq 0); the task file stays in tasks/ until its result is archived. */
const announce = (id: string) => writeFileSync(join(TMP, 'state', 'activity', `${id}.json`), JSON.stringify({ task_id: id, phase: 'RUNNING', seq: 0 }));
/** The core read the task file: the hook's processing row, nothing else yet. */
function coreReads(id: string) {
	announce(id);
	coreTool('Read', { file_path: join(TMP, 'tasks', `${id}.txt`) });
}
/** The core read the task and ran a tool on it: a working row. */
function coreWorks(id: string) {
	coreReads(id);
	coreTool('Bash', { command: 'true', description: 'Generate the image' });
}
const rows = (kind: string) => existsSync(join(TMP, 'state', 'agent-activity.jsonl'))
	? readFileSync(join(TMP, 'state', 'agent-activity.jsonl'), 'utf-8').split('\n').filter((l) => l.includes(`"kind": "${kind}"`)).length : 0;
const archivedTask = (id: string) => existsSync(join(TMP, 'tasks', 'archive', new Date().toISOString().slice(0, 7), `${id}.txt`));
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
	rmSync(join(TMP, 'state', 'agent-activity.jsonl'), { force: true });
	rmSync(join(TMP, 'state', 'agent-activity.sessions.json'), { force: true });
});

describe('cancel_task decides from the task state', () => {
	it('a queued task gets a cancel request: instruction written, task file deleted, card closed, no "Cancelled." result', async () => {
		const id = submit('draw a car');
		const out = await cancel();
		assert.equal(out.status, 'cancel_requested');
		assert.match(out.message, /Do not say it is cancelled/);
		assert.equal(out.taskId, id);
		assert.equal(cancelInstructions().length, 1);
		assert.ok(!existsSync(join(TMP, 'tasks', `${id}.txt`)));
		assert.ok(!archivedTask(id), 'not in tasks/archive/ either, where a core missing the file would find and run it');
		assert.ok(!existsSync(join(TMP, 'results', `${id}.txt`)), 'no stub result');
		assert.ok(statuses.some((s) => s.taskId === id && s.status === 'done' && s.text === 'Cancel requested.'));
		assert.ok(!_pendingTasksForTest.has(id), 'out of the timeout sweep');
	});

	it('a task announced but not yet read by the core is still queued', async () => {
		const id = submit('draw a statue');
		announce(id);
		assert.equal(voiceTaskState(id), 'queued');
		assert.equal((await cancel()).status, 'cancel_requested');
	});

	it('a task the core has read but not yet run a tool on is started, not cancelled', async () => {
		const id = submit('draw a dog');
		coreReads(id);
		assert.equal(rows('processing'), 1);
		assert.equal(rows('working'), 0);
		const out = await cancel();
		assert.equal(out.status, 'already_started');
		assert.deepEqual(cancelInstructions(), []);
		assert.ok(existsSync(join(TMP, 'tasks', `${id}.txt`)), 'the file the core has in context stays');
	});

	it('a task the core is working on is not "cancelled": no files, the user is told it will finish', async () => {
		const id = submit('draw a kiwi');
		coreWorks(id);
		assert.equal(rows('working'), 1);
		const out = await cancel();
		assert.equal(out.status, 'already_started');
		assert.match(out.message, /cannot stop partway/);
		assert.deepEqual(cancelInstructions(), []);
		assert.ok(!existsSync(join(TMP, 'results', `${id}.txt`)));
		assert.ok(_pendingTasksForTest.has(id), 'its result is still awaited');
	});

	it('a finished task (result written, not yet read) is left alone: the real result is not overwritten', async () => {
		const id = submit('draw a fence');
		coreWorks(id);
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

	it('a query also finds a voice task that already finished, and answers done, saying whether it was heard', async () => {
		const id = `task-${1_800_000_000_000 + ++seq}`;
		const month = new Date().toISOString().slice(0, 7);
		mkdirSync(join(TMP, 'results', 'archive', month), { recursive: true });
		writeFileSync(join(TMP, 'results', 'archive', month, `${id}.txt`), "Here's your mouse.");
		tb.voiceTaskStore.add(id, 'draw a mouse');
		tb.voiceTaskStore.set(id, 'spoken');
		const out = await cancel({ query: 'mouse' });
		assert.equal(out.status, 'already_done');
		assert.equal(out.taskId, id);
		assert.equal(out.heard as unknown as boolean, true);
		assert.deepEqual(cancelInstructions(), []);
	});

	it('a query matches the open task by its text', async () => {
		const dog = submit('draw a dog driving a car');
		submit('draw a parrot');
		assert.equal((await cancel({ query: 'dog' })).taskId, dog);
	});

	it("the core's reply to the cancel is spoken: it is the confirmation the user was promised", async () => {
		submit('draw an apple');
		await cancel();
		const [instruction] = cancelInstructions();
		writeFileSync(join(TMP, 'results', instruction), 'Cancelled task-x before it started.');
		await tick(2_500);
		assert.deepEqual(spoken.map((s) => s.text), ['Cancelled task-x before it started.']);
	});

	it('a cancelled task the core finished anyway is spoken with a note saying so', async () => {
		const id = submit('draw a boat');
		await cancel();
		writeFileSync(join(TMP, 'results', `${id}.txt`), "Here's your boat.");
		await tick(2_500);
		assert.deepEqual(spoken, [{ text: "Here's your boat.", note: CANCELLED_BUT_FINISHED_NOTE }]);
	});

	it('a task this session did not submit is never reported cancelled, and its file is left alone', async () => {
		const id = 'task-1800000000999';
		writeFileSync(join(TMP, 'tasks', `${id}.txt`), `id: ${id}\nsource: discord\nchannel_id: 123\ntask: summarize the thread\n`);
		const out = await cancel({ taskId: id });
		assert.equal(out.status, 'cancel_instruction_queued');
		assert.match(out.message, /not that it is cancelled/);
		assert.ok(existsSync(join(TMP, 'tasks', `${id}.txt`)));
		assert.equal(cancelInstructions().length, 1);
	});
});
