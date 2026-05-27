import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, existsSync, writeFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir, homedir } from 'node:os';

const { resolveWorkspace, statusPath, statusReadPath } = await import('../src/workspace_default.js');

// ── helpers ──────────────────────────────────────────────────────────────────

const TMP = join(tmpdir(), `sutando-workspace-test-${Date.now()}`);

const saved = { SUTANDO_WORKSPACE: process.env.SUTANDO_WORKSPACE };

function setWs(val: string | undefined) {
	if (val === undefined) delete process.env.SUTANDO_WORKSPACE;
	else process.env.SUTANDO_WORKSPACE = val;
}

function cleanTmp() {
	try { rmSync(TMP, { recursive: true, force: true }); } catch { /* best-effort */ }
}

after(() => {
	setWs(saved.SUTANDO_WORKSPACE);
	cleanTmp();
});

// ── resolveWorkspace ──────────────────────────────────────────────────────────

describe('resolveWorkspace', { concurrency: 1 }, () => {
	it('returns SUTANDO_WORKSPACE when set to an absolute path', () => {
		setWs('/tmp/my-workspace');
		assert.equal(resolveWorkspace(), '/tmp/my-workspace');
	});

	it('returns the default ~/.sutando/workspace when SUTANDO_WORKSPACE is unset', () => {
		setWs(undefined);
		assert.equal(resolveWorkspace(), join(homedir(), '.sutando', 'workspace'));
	});

	it('expands a leading ~ to homedir()', () => {
		setWs('~/my-workspace');
		assert.equal(resolveWorkspace(), join(homedir(), 'my-workspace'));
	});

	it('trims surrounding whitespace from the env var', () => {
		setWs('  /tmp/trimmed  ');
		assert.equal(resolveWorkspace(), '/tmp/trimmed');
	});

	it('does not expand ~ in the middle of the path', () => {
		setWs('/tmp/a~b');
		assert.equal(resolveWorkspace(), '/tmp/a~b');
	});
});

// ── statusPath ────────────────────────────────────────────────────────────────

describe('statusPath', { concurrency: 1 }, () => {
	it('returns <workspace>/state/<name> when workspace is provided', () => {
		const result = statusPath('core-status.json', '/tmp/ws');
		assert.equal(result, '/tmp/ws/state/core-status.json');
	});

	it('uses resolveWorkspace() when workspace argument is omitted', () => {
		setWs('/tmp/env-ws');
		const result = statusPath('core-status.json');
		assert.equal(result, '/tmp/env-ws/state/core-status.json');
	});

	it('nests the name under state/ regardless of name content', () => {
		const result = statusPath('loop-paused-until.sentinel', '/tmp/ws');
		assert.equal(result, '/tmp/ws/state/loop-paused-until.sentinel');
	});

	it('returns a different path from the legacy workspace root', () => {
		const wp = statusPath('core-status.json', '/tmp/ws');
		assert.notEqual(wp, '/tmp/ws/core-status.json');
	});
});

// ── statusReadPath ────────────────────────────────────────────────────────────

describe('statusReadPath', { concurrency: 1 }, () => {
	let ws: string;

	before(() => {
		ws = join(TMP, 'ws-srp');
		mkdirSync(join(ws, 'state'), { recursive: true });
	});

	it('returns the state/<name> path when that file exists', () => {
		const statefile = join(ws, 'state', 'exists.json');
		writeFileSync(statefile, '{}');
		const result = statusReadPath('exists.json', ws);
		assert.equal(result, statefile);
	});

	it('returns the legacy workspace-root path when only that file exists', () => {
		const legacyFile = join(ws, 'legacy-only.json');
		writeFileSync(legacyFile, '{}');
		const result = statusReadPath('legacy-only.json', ws);
		assert.equal(result, legacyFile);
	});

	it('returns the state/<name> path when neither file exists', () => {
		const result = statusReadPath('nonexistent.json', ws);
		assert.equal(result, join(ws, 'state', 'nonexistent.json'));
	});

	it('prefers state/<name> over the legacy root path when both exist', () => {
		const statefile = join(ws, 'state', 'both.json');
		const legacyFile = join(ws, 'both.json');
		writeFileSync(statefile, '{}');
		writeFileSync(legacyFile, '{}');
		const result = statusReadPath('both.json', ws);
		assert.equal(result, statefile);
	});

	it('uses resolveWorkspace() when workspace argument is omitted', () => {
		setWs('/tmp/env-ws-srp');
		const result = statusReadPath('core-status.json');
		// Neither file exists → returns state/ path
		assert.equal(result, '/tmp/env-ws-srp/state/core-status.json');
	});
});
