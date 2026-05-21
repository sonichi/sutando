import { describe, it, before, after, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, existsSync, unlinkSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { setupTempWorkspace } from './_helpers/temp-workspace.js';

// Unit tests for the get_core_status inline tool (PR #467).
// The tool reads `<SUTANDO_WORKSPACE>/state/core-status.json`. Tests run with
// SUTANDO_WORKSPACE pointing at a per-process temp dir so concurrent test
// files (notably tests/agent-state-endpoint.test.ts, which spawns a web-client
// that ALSO reads core-status.json) can't race. Before #840: both tests
// shared <REPO_ROOT>/core-status.json and node:test parallel-file mode caused
// intermittent failures.
const { workspace: TEMP_WORKSPACE, cleanup: cleanupTempWorkspace } =
	setupTempWorkspace('getcore');
mkdirSync(join(TEMP_WORKSPACE, 'state'), { recursive: true });
const CORE_STATUS_PATH = join(TEMP_WORKSPACE, 'state', 'core-status.json');

// Import AFTER setting SUTANDO_WORKSPACE so the tool's module-load resolves
// to the temp dir, not whatever the test process's env was before.
const { getCoreStatusTool } = await import('../src/inline-tools.js');

// Tool execute returns a plain object. Use any here because ToolDefinition
// typing makes the return a generic JsonValue.
async function invoke(): Promise<any> {
	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	return (getCoreStatusTool.execute as any)({}, null);
}

describe('get_core_status inline tool', () => {
	after(cleanupTempWorkspace);

	it('returns status:running with step + ageSec when fresh running file exists', async () => {
		const nowSec = Math.floor(Date.now() / 1000);
		writeFileSync(CORE_STATUS_PATH, JSON.stringify({ status: 'running', step: 'syncing memory', ts: nowSec - 5 }));
		const result = await invoke();
		assert.equal(result.status, 'running');
		assert.equal(result.step, 'syncing memory');
		assert.ok(result.ageSec >= 4 && result.ageSec <= 7, `ageSec should be ~5, got ${result.ageSec}`);
		assert.match(result.description, /working on: syncing memory/);
	});

	it('returns status:idle when status field is idle', async () => {
		writeFileSync(CORE_STATUS_PATH, JSON.stringify({ status: 'idle', ts: Math.floor(Date.now() / 1000) }));
		const result = await invoke();
		assert.equal(result.status, 'idle');
		assert.match(result.description, /idle/i);
	});

	it('returns status:idle when running file is older than 600s (TTL)', async () => {
		const staleSec = Math.floor(Date.now() / 1000) - 700;
		writeFileSync(CORE_STATUS_PATH, JSON.stringify({ status: 'running', step: 'ancient task', ts: staleSec }));
		const result = await invoke();
		assert.equal(result.status, 'idle', 'running with ts > 600s old should be treated as idle');
	});

	it('falls back to "(no step label)" when step is missing', async () => {
		writeFileSync(CORE_STATUS_PATH, JSON.stringify({ status: 'running', ts: Math.floor(Date.now() / 1000) }));
		const result = await invoke();
		assert.equal(result.status, 'running');
		assert.equal(result.step, '(no step label)');
	});

	it('returns status:idle when core-status.json is missing', async () => {
		try { unlinkSync(CORE_STATUS_PATH); } catch { /* already gone */ }
		const result = await invoke();
		assert.equal(result.status, 'idle');
		assert.match(result.description, /not currently running/i);
	});

	it('returns status:unknown when core-status.json is malformed JSON', async () => {
		writeFileSync(CORE_STATUS_PATH, '{ not valid json');
		const result = await invoke();
		assert.equal(result.status, 'unknown');
		assert.match(result.description, /could not read core status/i);
	});
});
