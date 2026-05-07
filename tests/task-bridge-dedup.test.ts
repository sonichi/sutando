import { describe, it, before, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, unlinkSync, mkdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
	workTool,
	_normalizeTaskTextForDedup,
	_findActivePendingByText,
	_resetPendingForTests,
} from '../src/task-bridge.js';

// Submit-side dedup invariants for issue #561. The voice `work` tool used
// to spawn parallel duplicate task files when the user repeated the same
// intent (because the previous submission was taking too long). After this
// fix, a same-text submission while the previous one is still pending
// returns the existing taskId and writes no new file.
//
// Test isolation: tests run in parallel by default, and task-bridge-format
// test's leak check would false-fire on any extra files we leave behind
// during interleaved execution. So we only call the file-writing path
// (`workTool.execute`) inside ONE focused test, with synchronous cleanup,
// and otherwise drive the dedup logic through the pure helpers
// (`_normalizeTaskTextForDedup`, `_findActivePendingByText`,
// `_resetPendingForTests`).

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const TASK_DIR = join(REPO_ROOT, 'tasks');

describe('_normalizeTaskTextForDedup', () => {
	it('lowercases and collapses whitespace', () => {
		assert.equal(_normalizeTaskTextForDedup('  Foo   BAR\tbaz  '), 'foo bar baz');
	});
	it('caps at 150 chars (defensive against giant inputs)', () => {
		const long = 'a'.repeat(200);
		assert.equal(_normalizeTaskTextForDedup(long).length, 150);
	});
	it('handles empty input', () => {
		assert.equal(_normalizeTaskTextForDedup(''), '');
	});
	it('treats case + whitespace variants as equal', () => {
		const a = _normalizeTaskTextForDedup('Summarize THE meeting notes');
		const b = _normalizeTaskTextForDedup('  summarize   the  MEETING\tnotes  ');
		assert.equal(a, b);
	});
});

describe('_findActivePendingByText (dedup decision)', () => {
	beforeEach(() => _resetPendingForTests());

	it('returns null when nothing pending', () => {
		assert.equal(_findActivePendingByText('foo'), null);
	});

	it('returns the matching taskId after a workTool submission', async () => {
		// One controlled file write — clean up immediately so format.test's
		// leak check never sees this file.
		const r = await workTool.execute({ task: 'dedup-helper-test-AAA' } as any, null as any) as any;
		try {
			assert.ok(r.taskId);
			assert.equal(_findActivePendingByText('dedup-helper-test-aaa'), r.taskId);
			assert.equal(_findActivePendingByText('something different'), null);
		} finally {
			try { unlinkSync(join(TASK_DIR, `${r.taskId}.txt`)); } catch {}
			_resetPendingForTests();
		}
	});

	it('case + whitespace variant of submitted text matches the pending taskId', async () => {
		const r = await workTool.execute({ task: 'Summarize THE meeting notes' } as any, null as any) as any;
		try {
			// _findActivePendingByText takes a *normalized* string — caller
			// (workTool.execute) does the normalization before lookup.
			const variantNormalized = _normalizeTaskTextForDedup('  summarize   the  MEETING\tnotes  ');
			assert.equal(_findActivePendingByText(variantNormalized), r.taskId);
		} finally {
			try { unlinkSync(join(TASK_DIR, `${r.taskId}.txt`)); } catch {}
			_resetPendingForTests();
		}
	});
});

describe('workTool dedup behavior (issue #561) — single-write scenario', () => {
	let createdTaskIds: string[] = [];

	before(() => {
		mkdirSync(TASK_DIR, { recursive: true });
	});

	beforeEach(() => {
		_resetPendingForTests();
		createdTaskIds = [];
	});

	afterEach(() => {
		for (const id of createdTaskIds) {
			try { unlinkSync(join(TASK_DIR, `${id}.txt`)); } catch {}
		}
		createdTaskIds = [];
	});

	async function submit(task: string): Promise<{ taskId: string; status: string; message?: string }> {
		const r = await workTool.execute({ task } as any, null as any) as any;
		assert.ok(r.taskId);
		if (!createdTaskIds.includes(r.taskId)) createdTaskIds.push(r.taskId);
		return r;
	}

	it('duplicate submission returns the same taskId; original file still on disk', async () => {
		const r1 = await submit('dedup-flow-BBB');
		const r2 = await submit('dedup-flow-BBB');

		assert.equal(r2.taskId, r1.taskId, 'second submission returns first taskId');
		assert.equal(r2.status, 'pending');
		assert.match(r2.message ?? '', /already working|in flight/i);
		// The single shared file (from r1) exists.
		assert.ok(existsSync(join(TASK_DIR, `${r1.taskId}.txt`)));
	});

	it('after pending is cleared (task completed), same text spawns fresh task', async () => {
		const r1 = await submit('dedup-flow-EEE');
		_resetPendingForTests(); // simulates result-watcher cleanup
		const r2 = await submit('dedup-flow-EEE');

		assert.notEqual(r2.taskId, r1.taskId, 'fresh taskId after pending was cleared');
	});
});
