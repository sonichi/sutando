import { describe, it, before, after, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, existsSync, unlinkSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { getCoreStatusTool } from '../src/inline-tools.js';

// Unit tests for the get_core_status inline tool (PR #467).
// The tool is an async fn that reads core-status.json via a hardcoded
// relative path (repo-root). Tests stash + restore any live value on the
// disk so a concurrent /proactive-loop pass doesn't corrupt the fixtures.

const CORE_STATUS_PATH = join(dirname(fileURLToPath(import.meta.url)), '..', 'core-status.json');
let saved: string | null = null;

// Tool execute returns a plain object. Use any here because ToolDefinition
// typing makes the return a generic JsonValue.
async function invoke(): Promise<any> {
	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	return (getCoreStatusTool.execute as any)({}, null);
}

describe('get_core_status inline tool', () => {
	before(() => {
		if (existsSync(CORE_STATUS_PATH)) {
			saved = readFileSync(CORE_STATUS_PATH, 'utf-8');
		}
	});

	after(() => {
		if (saved !== null) {
			writeFileSync(CORE_STATUS_PATH, saved);
		} else {
			try { unlinkSync(CORE_STATUS_PATH); } catch { /* idempotent */ }
		}
	});

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
