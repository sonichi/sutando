import { describe, it, before, after, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readdirSync, unlinkSync, mkdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { workTool, _pendingTasks } from '../src/task-bridge.js';

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const TASK_DIR = join(REPO_ROOT, 'tasks');

function listTaskFiles(): string[] {
	if (!existsSync(TASK_DIR)) return [];
	return readdirSync(TASK_DIR).filter(f => f.startsWith('task-') && f.endsWith('.txt'));
}

describe('task-bridge workTool — content dedup within window', () => {
	let baseline: Set<string>;

	before(() => {
		mkdirSync(TASK_DIR, { recursive: true });
		baseline = new Set(listTaskFiles());
		_pendingTasks.clear();
	});

	afterEach(() => {
		for (const fn of readdirSync(TASK_DIR)) {
			if (fn.startsWith('task-') && fn.endsWith('.txt') && !baseline.has(fn)) {
				try { unlinkSync(join(TASK_DIR, fn)); } catch { /* gone */ }
			}
		}
		_pendingTasks.clear();
	});

	after(() => { _pendingTasks.clear(); });

	it('reuses the same taskId for identical task strings within 5 minutes', async () => {
		const task = 'Check the status of PR #15 in the Boldai/real-time-agent repo.';
		const r1 = await workTool.execute({ task }, null) as { status: string; taskId: string };
		const r2 = await workTool.execute({ task }, null) as { status: string; taskId: string };
		assert.equal(r1.taskId, r2.taskId, 'duplicate same-content calls must collapse to one taskId');
		assert.equal(_pendingTasks.size, 1, 'only one pending entry should exist');
		// Filesystem should also have only one task file from this test
		const created = readdirSync(TASK_DIR).filter(f =>
			f.startsWith('task-') && f.endsWith('.txt') && !baseline.has(f)
		);
		assert.equal(created.length, 1, `exactly one task file expected, got ${created.length}`);
	});

	it('still creates a new task when content differs', async () => {
		const r1 = await workTool.execute({ task: 'task A' }, null) as { status: string; taskId: string };
		const r2 = await workTool.execute({ task: 'task B' }, null) as { status: string; taskId: string };
		assert.notEqual(r1.taskId, r2.taskId, 'different content should produce different taskIds');
		assert.equal(_pendingTasks.size, 2);
	});
});
