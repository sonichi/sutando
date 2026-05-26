import { describe, it, before, beforeEach, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync, readFileSync, existsSync, unlinkSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

// Set SUTANDO_WORKSPACE before import so module-level statePath constants resolve here.
// Clear SUTANDO_HOME so it doesn't intercept as priority-0 before SUTANDO_WORKSPACE.
const TMP = join(tmpdir(), `sutando-inline-cancel-test-${Date.now()}`);
mkdirSync(join(TMP, 'tasks'), { recursive: true });
mkdirSync(join(TMP, 'results'), { recursive: true });
mkdirSync(join(TMP, 'state'), { recursive: true });
delete process.env.SUTANDO_HOME;
process.env.SUTANDO_WORKSPACE = TMP;

const {
	cancelTaskTool,
	getCoreStatusTool,
	recentContextTool,
} = await import('../src/inline-tools.js');

// Core status lives under TMP/state/ when SUTANDO_WORKSPACE=TMP.
const CORE_STATUS_PATH = join(TMP, 'state', 'core-status.json');
const VOICE_CONTEXT_PATH = join(TMP, 'state', 'voice-session-context.json');
const TASKS_DIR = join(TMP, 'tasks');
const RESULTS_DIR = join(TMP, 'results');

// ── helpers ──────────────────────────────────────────────────────────────────

function writeTask(id: string, content: string) {
	writeFileSync(join(TASKS_DIR, `${id}.txt`), content);
}

function clearDir(dir: string) {
	try {
		for (const f of readdirSync(dir)) {
			if (f.endsWith('.txt')) unlinkSync(join(dir, f));
		}
	} catch { /* ignore */ }
}

function clearTasks() {
	clearDir(TASKS_DIR);
	clearDir(RESULTS_DIR);
}

// ── cancelTaskTool — list mode ────────────────────────────────────────────────

describe('cancelTaskTool — list mode', { concurrency: 1 }, () => {
	beforeEach(() => clearTasks());
	after(() => clearTasks());

	it('list:true with empty dir returns nothing_pending', async () => {
		const result = await cancelTaskTool.execute({ list: true }) as Record<string, unknown>;
		assert.equal(result.status, 'nothing_pending');
		assert.equal(result.count, 0);
		assert.deepEqual(result.tasks, []);
	});

	it('list:true with one task returns its id and preview', async () => {
		writeTask('task-9001', 'id: task-9001\ntimestamp: t\ntask: do something important\nsource: voice\n');
		const result = await cancelTaskTool.execute({ list: true }) as Record<string, unknown>;
		assert.equal(result.status, 'pending_tasks');
		assert.equal(result.count, 1);
		const tasks = result.tasks as Array<{ id: string; preview: string }>;
		assert.equal(tasks[0].id, 'task-9001');
		assert.ok(tasks[0].preview.includes('do something important'), 'preview should include task content');
	});

	it('list:true with multiple tasks returns all sorted', async () => {
		writeTask('task-1000', 'task: first task\n');
		writeTask('task-2000', 'task: second task\n');
		writeTask('task-3000', 'task: third task\n');
		const result = await cancelTaskTool.execute({ list: true }) as Record<string, unknown>;
		assert.equal(result.count, 3);
		const tasks = result.tasks as Array<{ id: string }>;
		assert.equal(tasks[0].id, 'task-1000');
		assert.equal(tasks[2].id, 'task-3000');
	});

	it('list:true truncates preview to 60 chars', async () => {
		const longContent = 'task: ' + 'x'.repeat(100) + '\n';
		writeTask('task-8888', longContent);
		const result = await cancelTaskTool.execute({ list: true }) as Record<string, unknown>;
		const tasks = result.tasks as Array<{ preview: string }>;
		assert.ok(tasks[0].preview.length <= 60, 'preview should be at most 60 chars');
	});

	it('listSources returns an array for tasks field', async () => {
		writeTask('task-5500', 'task: some task\n');
		const result = await cancelTaskTool.execute({ list: true }) as Record<string, unknown>;
		assert.ok(Array.isArray(result.tasks));
	});
});

// ── cancelTaskTool — default cancel (most recent) ─────────────────────────────

describe('cancelTaskTool — default cancel', { concurrency: 1 }, () => {
	beforeEach(() => clearTasks());
	after(() => clearTasks());

	it('returns nothing_pending when no tasks', async () => {
		const result = await cancelTaskTool.execute({}) as Record<string, unknown>;
		assert.equal(result.status, 'nothing_pending');
	});

	it('cancels most recent task and writes cancel-instruction', async () => {
		writeTask('task-5000', 'task: do something\n');
		writeTask('task-6000', 'task: do something else\n');

		const result = await cancelTaskTool.execute({}) as Record<string, unknown>;
		assert.equal(result.status, 'cancel_instruction_queued');
		assert.equal(result.taskId, 'task-6000');

		// A cancel-instruction file should exist in tasks/
		const tasksFiles = readdirSync(TASKS_DIR).filter((f: string) => f.endsWith('.txt'));
		const cancelFile = tasksFiles.find((f: string) => f !== 'task-5000.txt' && f !== 'task-6000.txt');
		assert.ok(cancelFile, 'cancel-instruction file should be written');
		const cancelContent = readFileSync(join(TASKS_DIR, cancelFile), 'utf-8');
		assert.ok(cancelContent.includes('CANCEL_INSTRUCTION'), 'should contain CANCEL_INSTRUCTION');

		// Original target file should be removed
		assert.ok(!existsSync(join(TASKS_DIR, 'task-6000.txt')), 'cancelled task file should be removed');

		// Cancelled result stub should exist
		assert.ok(existsSync(join(RESULTS_DIR, 'task-6000.txt')), 'cancelled result stub should be written');
	});
});

// ── cancelTaskTool — cancel by taskId ────────────────────────────────────────

describe('cancelTaskTool — cancel by taskId', { concurrency: 1 }, () => {
	beforeEach(() => clearTasks());
	after(() => clearTasks());

	it('cancels specific task by id', async () => {
		writeTask('task-7001', 'task: first\n');
		writeTask('task-7002', 'task: second\n');

		const result = await cancelTaskTool.execute({ taskId: 'task-7001' }) as Record<string, unknown>;
		assert.equal(result.status, 'cancel_instruction_queued');
		assert.equal(result.taskId, 'task-7001');
		assert.ok(!existsSync(join(TASKS_DIR, 'task-7001.txt')), 'task-7001 should be removed');
		assert.ok(existsSync(join(TASKS_DIR, 'task-7002.txt')), 'task-7002 should still exist');
	});

	it('accepts id without .txt extension', async () => {
		writeTask('task-7010', 'task: another\n');
		const result = await cancelTaskTool.execute({ taskId: 'task-7010' }) as Record<string, unknown>;
		assert.equal(result.status, 'cancel_instruction_queued');
		assert.equal(result.taskId, 'task-7010');
	});

	it('accepts cancel even for an already-gone task id (in-flight)', async () => {
		// task-9999 was never created — simulates in-flight case
		const result = await cancelTaskTool.execute({ taskId: 'task-9999' }) as Record<string, unknown>;
		assert.equal(result.status, 'cancel_instruction_queued');
		assert.equal(result.taskId, 'task-9999');
	});
});

// ── cancelTaskTool — cancel by query ─────────────────────────────────────────

describe('cancelTaskTool — cancel by query', { concurrency: 1 }, () => {
	beforeEach(() => clearTasks());
	after(() => clearTasks());

	it('finds and cancels task matching query substring', async () => {
		writeTask('task-8001', 'task: send email to Alice\n');
		writeTask('task-8002', 'task: book flight\n');

		const result = await cancelTaskTool.execute({ query: 'alice' }) as Record<string, unknown>;
		assert.equal(result.status, 'cancel_instruction_queued');
		assert.equal(result.taskId, 'task-8001');
		assert.ok(!existsSync(join(TASKS_DIR, 'task-8001.txt')), 'matched task should be removed');
	});

	it('returns not_found when no task matches query', async () => {
		writeTask('task-8010', 'task: buy groceries\n');
		const result = await cancelTaskTool.execute({ query: 'unicorn' }) as Record<string, unknown>;
		assert.equal(result.status, 'not_found');
		assert.equal(result.query, 'unicorn');
	});

	it('returns nothing_pending when tasks dir is empty', async () => {
		const result = await cancelTaskTool.execute({ query: 'anything' }) as Record<string, unknown>;
		assert.equal(result.status, 'nothing_pending');
	});

	it('query match is case-insensitive', async () => {
		writeTask('task-8020', 'task: call Bob for update\n');
		const result = await cancelTaskTool.execute({ query: 'CALL BOB' }) as Record<string, unknown>;
		assert.equal(result.status, 'cancel_instruction_queued');
	});
});

// ── getCoreStatusTool ─────────────────────────────────────────────────────────

describe('getCoreStatusTool', { concurrency: 1 }, () => {
	const originalContent = existsSync(CORE_STATUS_PATH)
		? readFileSync(CORE_STATUS_PATH, 'utf-8')
		: null;

	after(() => {
		if (originalContent !== null) {
			writeFileSync(CORE_STATUS_PATH, originalContent);
		} else {
			try { unlinkSync(CORE_STATUS_PATH); } catch {}
		}
	});

	it('returns idle when core-status.json is absent', async () => {
		try { unlinkSync(CORE_STATUS_PATH); } catch {}
		const result = await getCoreStatusTool.execute({}) as Record<string, unknown>;
		assert.equal(result.status, 'idle');
	});

	it('returns idle when status is idle in file', async () => {
		writeFileSync(CORE_STATUS_PATH, JSON.stringify({ status: 'idle', ts: Math.floor(Date.now() / 1000) }));
		const result = await getCoreStatusTool.execute({}) as Record<string, unknown>;
		assert.equal(result.status, 'idle');
	});

	it('returns running when status is running and ts is recent', async () => {
		const recentTs = Math.floor(Date.now() / 1000) - 10;
		writeFileSync(CORE_STATUS_PATH, JSON.stringify({ status: 'running', ts: recentTs, step: 'testing core status' }));
		const result = await getCoreStatusTool.execute({}) as Record<string, unknown>;
		assert.equal(result.status, 'running');
		assert.equal(result.step, 'testing core status');
		assert.ok(typeof result.ageSec === 'number', 'ageSec should be a number');
		assert.ok((result.ageSec as number) < 600, 'ageSec should be recent');
	});

	it('returns idle when status is running but ts is stale (>600s)', async () => {
		const staleTs = Math.floor(Date.now() / 1000) - 700;
		writeFileSync(CORE_STATUS_PATH, JSON.stringify({ status: 'running', ts: staleTs, step: 'old step' }));
		const result = await getCoreStatusTool.execute({}) as Record<string, unknown>;
		assert.equal(result.status, 'idle');
	});

	it('returns unknown when file has malformed JSON', async () => {
		writeFileSync(CORE_STATUS_PATH, 'not-valid-json{{{');
		const result = await getCoreStatusTool.execute({}) as Record<string, unknown>;
		assert.equal(result.status, 'unknown');
		assert.ok(typeof result.description === 'string');
	});

	it('description field is always a string', async () => {
		writeFileSync(CORE_STATUS_PATH, JSON.stringify({ status: 'idle', ts: Math.floor(Date.now() / 1000) }));
		const result = await getCoreStatusTool.execute({}) as Record<string, unknown>;
		assert.ok(typeof result.description === 'string', 'description should always be a string');
	});
});

// ── recentContextTool ─────────────────────────────────────────────────────────

describe('recentContextTool', { concurrency: 1 }, () => {
	beforeEach(() => { try { unlinkSync(VOICE_CONTEXT_PATH); } catch {} });
	after(() => { try { unlinkSync(VOICE_CONTEXT_PATH); } catch {} });

	it('returns note when context file is missing', async () => {
		const result = await recentContextTool.execute({}) as Record<string, unknown>;
		assert.ok('note' in result, 'should have a note key when file is missing');
		assert.ok((result.note as string).includes('no context'), 'note should mention no context');
	});

	it('returns parsed JSON when file exists', async () => {
		const ctx = {
			active_drafts: [{ id: 'draft-1', content: 'test' }],
			pending_action: null,
			last_results: [{ task_id: 'task-123', subject: 'email', ts: 1700000000 }],
			updated_at: '2026-05-24T08:00:00Z',
		};
		writeFileSync(VOICE_CONTEXT_PATH, JSON.stringify(ctx));
		const result = await recentContextTool.execute({}) as Record<string, unknown>;
		assert.deepEqual(result.active_drafts, ctx.active_drafts);
		assert.deepEqual(result.last_results, ctx.last_results);
		assert.equal(result.updated_at, ctx.updated_at);
	});

	it('returns error when file has invalid JSON', async () => {
		writeFileSync(VOICE_CONTEXT_PATH, 'broken-json{{');
		const result = await recentContextTool.execute({}) as Record<string, unknown>;
		assert.ok('error' in result, 'should return error for invalid JSON');
	});
});
