import { describe, it, before, after, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync, unlinkSync, mkdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { workTool } from '../src/task-bridge.js';

// Integration test for PR #460's unified task-file schema. Every voice /
// work-tool task should emit the same set of fields the Discord bridge
// emits, so downstream consumers (Claude Code session, access-tier
// sandboxing) can treat all tasks uniformly.

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const TASK_DIR = join(REPO_ROOT, 'tasks');

function listTaskFiles(): string[] {
	if (!existsSync(TASK_DIR)) return [];
	return readdirSync(TASK_DIR).filter(f => f.startsWith('task-') && f.endsWith('.txt'));
}

describe('task-bridge workTool — PR #460 unified format', () => {
	let createdFiles: string[] = [];
	let baselineFiles: Set<string>;

	before(() => {
		mkdirSync(TASK_DIR, { recursive: true });
		baselineFiles = new Set(listTaskFiles());
	});

	afterEach(() => {
		// Clean up only the files we created; leave prod task files alone.
		for (const fn of createdFiles) {
			try { unlinkSync(join(TASK_DIR, fn)); } catch { /* already gone */ }
		}
		createdFiles = [];
	});

	after(() => {
		// Leak check: after all tests + cleanup, only baseline files should
		// remain. Anything extra means a test wrote a file but didn't track
		// it through createdFiles (e.g. a future test that bypasses
		// invokeWorkTool). Surfaces gaps in the cleanup harness early.
		const final = new Set(listTaskFiles());
		const leaked: string[] = [];
		for (const f of final) if (!baselineFiles.has(f)) leaked.push(f);
		assert.deepEqual(leaked, [], 'test leaked task files: ' + leaked.join(', '));
	});

	async function invokeWorkTool(task: string): Promise<string> {
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const result = await (workTool.execute as any)({ task }, null) as { taskId?: string };
		assert.ok(result.taskId, 'workTool should return a taskId');
		const fn = result.taskId + '.txt';
		createdFiles.push(fn);
		return fn;
	}

	it('writes all 7 fields required by the Discord-bridge schema', async () => {
		const fn = await invokeWorkTool('Test task from format unit test');
		const content = readFileSync(join(TASK_DIR, fn), 'utf-8');
		// Each of these is the schema locked in by PR #460.
		assert.match(content, /^id: task-\d+$/m, 'id field');
		assert.match(content, /^timestamp: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/m, 'ISO8601 timestamp');
		assert.match(content, /^task: Test task from format unit test$/m, 'task body');
		assert.match(content, /^source: voice$/m, 'source=voice for workTool');
		assert.match(content, /^channel_id: local-voice$/m, 'channel_id=local-voice');
		assert.match(content, /^user_id: \S+$/m, 'user_id (env or voice-local fallback)');
		assert.match(content, /^access_tier: owner$/m, 'access_tier=owner (voice is owner-only)');
	});

	it('does NOT emit the legacy "reminder:" field dropped in PR #460', async () => {
		const fn = await invokeWorkTool('Test task — no reminder');
		const content = readFileSync(join(TASK_DIR, fn), 'utf-8');
		assert.doesNotMatch(content, /^reminder:/m, 'reminder field was dropped');
	});

	it('uses SUTANDO_DM_OWNER_ID when set, falls back to voice-local sentinel', async () => {
		// Case 1: default (env unset) → voice-local
		const fn1 = await invokeWorkTool('default fallback');
		const c1 = readFileSync(join(TASK_DIR, fn1), 'utf-8');
		if (!process.env.SUTANDO_DM_OWNER_ID) {
			assert.match(c1, /^user_id: voice-local$/m);
		} else {
			assert.match(c1, new RegExp(`^user_id: ${process.env.SUTANDO_DM_OWNER_ID}$`, 'm'));
		}
	});

	it('generates unique task IDs for back-to-back calls', async () => {
		const fn1 = await invokeWorkTool('first');
		const fn2 = await invokeWorkTool('second');
		assert.notEqual(fn1, fn2, 'task IDs must differ');
	});
});
