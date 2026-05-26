// Tests for personalPath, sharedPersonalPath, claudeHomePath in src/util_paths.ts.
//
// memoryDirEnv is already covered by util-paths-memory-dir.test.ts.
// This file focuses on the three path-resolver exports that do existsSync-based
// fallback logic. All helpers read env vars inside the function body, so we can
// control them via process.env at call time without any pre-import dance.

import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { hostname } from 'node:os';

import { personalPath, sharedPersonalPath, claudeHomePath } from '../src/util_paths.js';

const HOST = hostname().split('.')[0];

// ── helpers ───────────────────────────────────────────────────────────────────

function touch(p: string): void {
	mkdirSync(join(p, '..'), { recursive: true });
	writeFileSync(p, '');
}

// Save/restore the env vars touched by these tests.
function saveEnv() {
	return {
		SUTANDO_MEMORY_DIR: process.env.SUTANDO_MEMORY_DIR,
		SUTANDO_PRIVATE_DIR: process.env.SUTANDO_PRIVATE_DIR,
		SUTANDO_WORKSPACE: process.env.SUTANDO_WORKSPACE,
		CLAUDE_HOME: process.env.CLAUDE_HOME,
	};
}

function restoreEnv(saved: ReturnType<typeof saveEnv>) {
	for (const [k, v] of Object.entries(saved)) {
		if (v === undefined) delete process.env[k as keyof typeof saved];
		else process.env[k as keyof typeof saved] = v;
	}
}

// ── personalPath ──────────────────────────────────────────────────────────────

describe('personalPath', { concurrency: 1 }, () => {
	let TMP: string;
	let saved: ReturnType<typeof saveEnv>;

	before(() => {
		TMP = join(tmpdir(), `sutando-util-paths-test-${Date.now()}`);
		mkdirSync(TMP, { recursive: true });
		saved = saveEnv();
	});

	after(() => {
		restoreEnv(saved);
		try { rmSync(TMP, { recursive: true, force: true }); } catch { /* best-effort */ }
	});

	it('returns memory-dir machine path when file exists there', () => {
		const memDir = join(TMP, 'mem-a');
		const machineDir = join(memDir, `machine-${HOST}`);
		mkdirSync(machineDir, { recursive: true });
		touch(join(machineDir, 'test.json'));

		process.env.SUTANDO_MEMORY_DIR = memDir;
		delete process.env.SUTANDO_PRIVATE_DIR;

		const result = personalPath('test.json', join(TMP, 'ws'));
		assert.equal(result, join(machineDir, 'test.json'));
	});

	it('falls back to workspace path when memory-dir file absent but ws file exists', () => {
		const memDir = join(TMP, 'mem-b');
		mkdirSync(join(memDir, `machine-${HOST}`), { recursive: true });
		// Do NOT create the file in memDir — only in workspace.
		const ws = join(TMP, 'ws-b');
		mkdirSync(ws, { recursive: true });
		touch(join(ws, 'fallback.json'));

		process.env.SUTANDO_MEMORY_DIR = memDir;
		delete process.env.SUTANDO_PRIVATE_DIR;

		const result = personalPath('fallback.json', ws);
		assert.equal(result, join(ws, 'fallback.json'));
	});

	it('returns memory-dir machine path when neither file exists (fail-graceful return)', () => {
		const memDir = join(TMP, 'mem-c');
		mkdirSync(memDir, { recursive: true });

		process.env.SUTANDO_MEMORY_DIR = memDir;
		delete process.env.SUTANDO_PRIVATE_DIR;

		const result = personalPath('ghost.json', join(TMP, 'ws-c'));
		// Should point at the preferred private location (for caller's existsSync check).
		assert.equal(result, join(memDir, `machine-${HOST}`, 'ghost.json'));
	});

	it('returns workspace path when SUTANDO_MEMORY_DIR not set and no file exists', () => {
		delete process.env.SUTANDO_MEMORY_DIR;
		delete process.env.SUTANDO_PRIVATE_DIR;

		const ws = join(TMP, 'ws-d');
		const result = personalPath('nofile.json', ws);
		assert.equal(result, join(ws, 'nofile.json'));
	});

	it('returns workspace path when SUTANDO_MEMORY_DIR not set and ws file exists', () => {
		delete process.env.SUTANDO_MEMORY_DIR;
		delete process.env.SUTANDO_PRIVATE_DIR;

		const ws = join(TMP, 'ws-e');
		mkdirSync(ws, { recursive: true });
		touch(join(ws, 'present.json'));

		const result = personalPath('present.json', ws);
		assert.equal(result, join(ws, 'present.json'));
	});

	it('stand-avatar.png: prefers memory-dir when file exists there', () => {
		const memDir = join(TMP, 'mem-f');
		const machineDir = join(memDir, `machine-${HOST}`);
		mkdirSync(machineDir, { recursive: true });
		touch(join(machineDir, 'stand-avatar.png'));

		process.env.SUTANDO_MEMORY_DIR = memDir;
		delete process.env.SUTANDO_PRIVATE_DIR;

		const result = personalPath('stand-avatar.png', join(TMP, 'ws-f'));
		assert.equal(result, join(machineDir, 'stand-avatar.png'));
	});

	it('stand-avatar.png: falls back to assets/ subdir in workspace when not in memory-dir', () => {
		const memDir = join(TMP, 'mem-g');
		mkdirSync(join(memDir, `machine-${HOST}`), { recursive: true });
		// No avatar in memDir.

		const ws = join(TMP, 'ws-g');
		const assetsDir = join(ws, 'assets');
		mkdirSync(assetsDir, { recursive: true });
		touch(join(assetsDir, 'stand-avatar.png'));

		process.env.SUTANDO_MEMORY_DIR = memDir;
		delete process.env.SUTANDO_PRIVATE_DIR;

		const result = personalPath('stand-avatar.png', ws);
		assert.equal(result, join(assetsDir, 'stand-avatar.png'));
	});

	it('stand-avatar.png: returns assets/ path when nothing exists', () => {
		delete process.env.SUTANDO_MEMORY_DIR;
		delete process.env.SUTANDO_PRIVATE_DIR;

		const ws = join(TMP, 'ws-h');
		mkdirSync(ws, { recursive: true });

		const result = personalPath('stand-avatar.png', ws);
		assert.equal(result, join(ws, 'assets', 'stand-avatar.png'));
	});
});

// ── sharedPersonalPath ────────────────────────────────────────────────────────

describe('sharedPersonalPath', { concurrency: 1 }, () => {
	let TMP: string;
	let saved: ReturnType<typeof saveEnv>;

	before(() => {
		TMP = join(tmpdir(), `sutando-shared-path-test-${Date.now()}`);
		mkdirSync(TMP, { recursive: true });
		saved = saveEnv();
	});

	after(() => {
		restoreEnv(saved);
		try { rmSync(TMP, { recursive: true, force: true }); } catch { /* best-effort */ }
	});

	it('returns memory-dir path when file exists there', () => {
		const memDir = join(TMP, 'mem-s-a');
		mkdirSync(memDir, { recursive: true });
		touch(join(memDir, 'shared.md'));

		process.env.SUTANDO_MEMORY_DIR = memDir;
		delete process.env.SUTANDO_PRIVATE_DIR;

		const result = sharedPersonalPath('shared.md', join(TMP, 'ws-s-a'));
		assert.equal(result, join(memDir, 'shared.md'));
	});

	it('falls back to workspace path when memory-dir file absent but ws file exists', () => {
		const memDir = join(TMP, 'mem-s-b');
		mkdirSync(memDir, { recursive: true });
		const ws = join(TMP, 'ws-s-b');
		mkdirSync(ws, { recursive: true });
		touch(join(ws, 'shared.md'));

		process.env.SUTANDO_MEMORY_DIR = memDir;
		delete process.env.SUTANDO_PRIVATE_DIR;

		const result = sharedPersonalPath('shared.md', ws);
		assert.equal(result, join(ws, 'shared.md'));
	});

	it('returns memory-dir candidate when neither exists (fail-graceful)', () => {
		const memDir = join(TMP, 'mem-s-c');
		mkdirSync(memDir, { recursive: true });

		process.env.SUTANDO_MEMORY_DIR = memDir;
		delete process.env.SUTANDO_PRIVATE_DIR;

		const result = sharedPersonalPath('ghost.md', join(TMP, 'ws-s-c'));
		assert.equal(result, join(memDir, 'ghost.md'));
	});

	it('returns workspace path when SUTANDO_MEMORY_DIR not set', () => {
		delete process.env.SUTANDO_MEMORY_DIR;
		delete process.env.SUTANDO_PRIVATE_DIR;

		const ws = join(TMP, 'ws-s-d');
		const result = sharedPersonalPath('shared.md', ws);
		assert.equal(result, join(ws, 'shared.md'));
	});
});

// ── claudeHomePath ────────────────────────────────────────────────────────────

describe('claudeHomePath', { concurrency: 1 }, () => {
	let saved: ReturnType<typeof saveEnv>;

	before(() => { saved = saveEnv(); });
	after(() => { restoreEnv(saved); });

	it('returns ~/.claude when called with no args and CLAUDE_HOME not set', () => {
		delete process.env.CLAUDE_HOME;
		const result = claudeHomePath();
		assert.ok(result.endsWith('/.claude'), `expected path ending in /.claude, got: ${result}`);
	});

	it('returns $CLAUDE_HOME when set and called with no args', () => {
		process.env.CLAUDE_HOME = '/tmp/my-claude-home';
		const result = claudeHomePath();
		assert.equal(result, '/tmp/my-claude-home');
	});

	it('joins subpath components under ~/.claude', () => {
		delete process.env.CLAUDE_HOME;
		const result = claudeHomePath('channels', 'discord', 'access.json');
		assert.ok(result.endsWith('/.claude/channels/discord/access.json'), `got: ${result}`);
	});

	it('joins subpath components under $CLAUDE_HOME', () => {
		process.env.CLAUDE_HOME = '/tmp/alt-claude';
		const result = claudeHomePath('skills', 'morning-briefing');
		assert.equal(result, '/tmp/alt-claude/skills/morning-briefing');
	});

	it('expands ~ in CLAUDE_HOME', () => {
		process.env.CLAUDE_HOME = '~/.my-claude';
		const result = claudeHomePath();
		assert.ok(!result.startsWith('~'), `tilde was not expanded: ${result}`);
		assert.ok(result.includes('.my-claude'), `got: ${result}`);
	});

	it('single subpath component returns correct join', () => {
		process.env.CLAUDE_HOME = '/tmp/c';
		const result = claudeHomePath('settings.json');
		assert.equal(result, '/tmp/c/settings.json');
	});
});
