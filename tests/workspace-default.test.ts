import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

/**
 * Tests for src/workspace_default.ts — the canonical workspace-path resolver
 * used by every TS service. Covers resolveWorkspace(), statusPath(), and
 * statusReadPath() (including its legacy fallback for un-migrated installs).
 *
 * Covers:
 *   resolveWorkspace():
 *     1. No env var → ~/.sutando/workspace/ default.
 *     2. SUTANDO_WORKSPACE set → that value returned.
 *     3. SUTANDO_WORKSPACE with leading ~ → tilde expanded.
 *   statusPath():
 *     4. Returns <workspace>/state/<name>.
 *     5. Accepts explicit workspace arg.
 *   statusReadPath() (fs-checked):
 *     6. File at state/<name> → state/ path returned.
 *     7. File at root only (legacy) → root path returned.
 *     8. Neither exists → state/ path returned (default, no false positive).
 *     9. Both exist → state/ preferred (migration complete path).
 */

import { resolveWorkspace, statusPath, statusReadPath } from '../src/workspace_default.js';

function clearWorkspaceEnv() {
	delete process.env.SUTANDO_WORKSPACE;
}

describe('resolveWorkspace()', () => {
	it('returns ~/.sutando/workspace when SUTANDO_WORKSPACE is not set', () => {
		clearWorkspaceEnv();
		const result = resolveWorkspace();
		assert.equal(result, join(homedir(), '.sutando', 'workspace'));
	});

	it('returns SUTANDO_WORKSPACE value verbatim when set', () => {
		process.env.SUTANDO_WORKSPACE = '/tmp/custom-ws';
		try {
			assert.equal(resolveWorkspace(), '/tmp/custom-ws');
		} finally {
			clearWorkspaceEnv();
		}
	});

	it('expands leading ~ in SUTANDO_WORKSPACE', () => {
		process.env.SUTANDO_WORKSPACE = '~/my-ws';
		try {
			const result = resolveWorkspace();
			assert.ok(!result.startsWith('~'), 'tilde should be expanded to home dir');
			assert.ok(result.startsWith(homedir()), 'expanded path should start with homedir');
			assert.ok(result.endsWith('/my-ws'));
		} finally {
			clearWorkspaceEnv();
		}
	});

	it('trims whitespace from SUTANDO_WORKSPACE', () => {
		process.env.SUTANDO_WORKSPACE = '  /tmp/ws-trimmed  ';
		try {
			assert.equal(resolveWorkspace(), '/tmp/ws-trimmed');
		} finally {
			clearWorkspaceEnv();
		}
	});
});

describe('statusPath()', () => {
	it('returns <workspace>/state/<name> using resolved workspace', () => {
		process.env.SUTANDO_WORKSPACE = '/tmp/test-ws';
		try {
			assert.equal(statusPath('core-status.json'), '/tmp/test-ws/state/core-status.json');
		} finally {
			clearWorkspaceEnv();
		}
	});

	it('uses the explicit workspace arg instead of env', () => {
		process.env.SUTANDO_WORKSPACE = '/tmp/should-be-ignored';
		try {
			assert.equal(statusPath('foo.json', '/custom/ws'), '/custom/ws/state/foo.json');
		} finally {
			clearWorkspaceEnv();
		}
	});
});

describe('statusReadPath() — legacy fallback', () => {
	let tmpWs: string;

	before(() => {
		tmpWs = join(tmpdir(), `sutando-ws-test-${Date.now()}`);
		mkdirSync(join(tmpWs, 'state'), { recursive: true });
	});

	after(() => {
		rmSync(tmpWs, { recursive: true, force: true });
	});

	it('returns state/ path when file exists there', () => {
		writeFileSync(join(tmpWs, 'state', 'status.json'), '{}');
		assert.equal(statusReadPath('status.json', tmpWs), join(tmpWs, 'state', 'status.json'));
	});

	it('falls back to legacy root path when state/ file absent but root file present', () => {
		writeFileSync(join(tmpWs, 'legacy.json'), '{}');
		assert.equal(statusReadPath('legacy.json', tmpWs), join(tmpWs, 'legacy.json'));
	});

	it('returns state/ path when neither file exists (no false positive)', () => {
		assert.equal(
			statusReadPath('nonexistent.json', tmpWs),
			join(tmpWs, 'state', 'nonexistent.json'),
		);
	});

	it('prefers state/ path when both state/ and root files exist', () => {
		writeFileSync(join(tmpWs, 'state', 'both.json'), '{}');
		writeFileSync(join(tmpWs, 'both.json'), '{}');
		assert.equal(statusReadPath('both.json', tmpWs), join(tmpWs, 'state', 'both.json'));
	});
});
