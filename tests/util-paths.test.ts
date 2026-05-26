import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync, rmSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir, hostname } from 'node:os';

// util_paths reads env at call time (no module-level capture),
// so we can mutate env vars freely before each test.
const {
	memoryDirEnv,
	personalPath,
	sharedPersonalPath,
	claudeHomePath,
} = await import('../src/util_paths.js');

const TMP = join(tmpdir(), `sutando-util-paths-test-${Date.now()}`);
const HOST = hostname().split('.')[0];

// Snapshot env vars we'll mutate so we can restore them.
const originalMemoryDir = process.env.SUTANDO_MEMORY_DIR;
const originalPrivateDir = process.env.SUTANDO_PRIVATE_DIR;
const originalWorkspace = process.env.SUTANDO_WORKSPACE;
const originalClaudeHome = process.env.CLAUDE_HOME;

after(() => {
	// Restore env
	if (originalMemoryDir !== undefined) process.env.SUTANDO_MEMORY_DIR = originalMemoryDir;
	else delete process.env.SUTANDO_MEMORY_DIR;
	if (originalPrivateDir !== undefined) process.env.SUTANDO_PRIVATE_DIR = originalPrivateDir;
	else delete process.env.SUTANDO_PRIVATE_DIR;
	if (originalWorkspace !== undefined) process.env.SUTANDO_WORKSPACE = originalWorkspace;
	else delete process.env.SUTANDO_WORKSPACE;
	if (originalClaudeHome !== undefined) process.env.CLAUDE_HOME = originalClaudeHome;
	else delete process.env.CLAUDE_HOME;
	// Cleanup temp dir
	try { rmSync(TMP, { recursive: true, force: true }); } catch {}
});

// ── memoryDirEnv ──────────────────────────────────────────────────────────────

describe('memoryDirEnv', { concurrency: 1 }, () => {
	it('returns undefined when neither env var is set', () => {
		delete process.env.SUTANDO_MEMORY_DIR;
		delete process.env.SUTANDO_PRIVATE_DIR;
		assert.equal(memoryDirEnv(), undefined);
	});

	it('returns SUTANDO_MEMORY_DIR when set', () => {
		process.env.SUTANDO_MEMORY_DIR = '/some/memory/dir';
		delete process.env.SUTANDO_PRIVATE_DIR;
		assert.equal(memoryDirEnv(), '/some/memory/dir');
		delete process.env.SUTANDO_MEMORY_DIR;
	});

	it('returns SUTANDO_PRIVATE_DIR when SUTANDO_MEMORY_DIR is not set', () => {
		delete process.env.SUTANDO_MEMORY_DIR;
		process.env.SUTANDO_PRIVATE_DIR = '/legacy/private/dir';
		assert.equal(memoryDirEnv(), '/legacy/private/dir');
		delete process.env.SUTANDO_PRIVATE_DIR;
	});

	it('prefers SUTANDO_MEMORY_DIR over SUTANDO_PRIVATE_DIR when both set', () => {
		process.env.SUTANDO_MEMORY_DIR = '/new/memory';
		process.env.SUTANDO_PRIVATE_DIR = '/old/private';
		assert.equal(memoryDirEnv(), '/new/memory');
		delete process.env.SUTANDO_MEMORY_DIR;
		delete process.env.SUTANDO_PRIVATE_DIR;
	});
});

// ── claudeHomePath ────────────────────────────────────────────────────────────

describe('claudeHomePath', { concurrency: 1 }, () => {
	it('returns ~/.claude when called with no args and CLAUDE_HOME not set', () => {
		delete process.env.CLAUDE_HOME;
		const result = claudeHomePath();
		assert.ok(result.endsWith('/.claude'), `should end with /.claude, got: ${result}`);
	});

	it('returns CLAUDE_HOME base when set', () => {
		process.env.CLAUDE_HOME = '/custom/claude/home';
		const result = claudeHomePath();
		assert.equal(result, '/custom/claude/home');
		delete process.env.CLAUDE_HOME;
	});

	it('joins subpath components correctly', () => {
		process.env.CLAUDE_HOME = '/test/claude';
		const result = claudeHomePath('channels', 'discord', 'access.json');
		assert.equal(result, '/test/claude/channels/discord/access.json');
		delete process.env.CLAUDE_HOME;
	});

	it('expands ~ in CLAUDE_HOME', () => {
		process.env.CLAUDE_HOME = '~/my-claude';
		const result = claudeHomePath();
		assert.ok(!result.startsWith('~'), 'should expand tilde');
		assert.ok(result.includes('my-claude'), 'should contain the path after tilde');
		delete process.env.CLAUDE_HOME;
	});

	it('single subpath component appended to base', () => {
		process.env.CLAUDE_HOME = '/base';
		assert.equal(claudeHomePath('skills'), '/base/skills');
		delete process.env.CLAUDE_HOME;
	});
});

// ── sharedPersonalPath ────────────────────────────────────────────────────────

describe('sharedPersonalPath', { concurrency: 1 }, () => {
	before(() => {
		mkdirSync(join(TMP, 'memory'), { recursive: true });
		mkdirSync(join(TMP, 'workspace'), { recursive: true });
	});

	it('returns workspace/filename when no SUTANDO_MEMORY_DIR set', () => {
		delete process.env.SUTANDO_MEMORY_DIR;
		delete process.env.SUTANDO_PRIVATE_DIR;
		const ws = join(TMP, 'workspace');
		const result = sharedPersonalPath('build_log.md', ws);
		assert.equal(result, join(ws, 'build_log.md'));
	});

	it('returns memory/filename (preferred path) when SUTANDO_MEMORY_DIR set but file absent', () => {
		process.env.SUTANDO_MEMORY_DIR = join(TMP, 'memory');
		const ws = join(TMP, 'workspace');
		const result = sharedPersonalPath('nonexistent.md', ws);
		assert.equal(result, join(TMP, 'memory', 'nonexistent.md'));
		delete process.env.SUTANDO_MEMORY_DIR;
	});

	it('returns memory/filename when SUTANDO_MEMORY_DIR is set and file exists there', () => {
		process.env.SUTANDO_MEMORY_DIR = join(TMP, 'memory');
		const ws = join(TMP, 'workspace');
		const targetFile = join(TMP, 'memory', 'shared-file.md');
		writeFileSync(targetFile, 'content');
		const result = sharedPersonalPath('shared-file.md', ws);
		assert.equal(result, targetFile);
		delete process.env.SUTANDO_MEMORY_DIR;
	});

	it('returns workspace/filename when memory dir set but file in workspace only', () => {
		process.env.SUTANDO_MEMORY_DIR = join(TMP, 'memory');
		const ws = join(TMP, 'workspace');
		const wsFile = join(ws, 'ws-only-file.md');
		writeFileSync(wsFile, 'ws content');
		const result = sharedPersonalPath('ws-only-file.md', ws);
		// File exists in workspace but not in memory dir → returns workspace path
		assert.equal(result, wsFile);
		delete process.env.SUTANDO_MEMORY_DIR;
	});
});

// ── personalPath ──────────────────────────────────────────────────────────────

describe('personalPath', { concurrency: 1 }, () => {
	before(() => {
		mkdirSync(join(TMP, 'machine-memory', `machine-${HOST}`), { recursive: true });
		mkdirSync(join(TMP, 'machine-ws'), { recursive: true });
		mkdirSync(join(TMP, 'machine-ws', 'assets'), { recursive: true });
	});

	it('returns workspace/filename when no SUTANDO_MEMORY_DIR set and file absent', () => {
		delete process.env.SUTANDO_MEMORY_DIR;
		delete process.env.SUTANDO_PRIVATE_DIR;
		const ws = join(TMP, 'machine-ws');
		const result = personalPath('my-config.json', ws);
		assert.equal(result, join(ws, 'my-config.json'));
	});

	it('returns machine-<host>/filename when SUTANDO_MEMORY_DIR set and per-machine file exists', () => {
		process.env.SUTANDO_MEMORY_DIR = join(TMP, 'machine-memory');
		const ws = join(TMP, 'machine-ws');
		const machineFile = join(TMP, 'machine-memory', `machine-${HOST}`, 'stand-identity.json');
		writeFileSync(machineFile, '{}');
		const result = personalPath('stand-identity.json', ws);
		assert.equal(result, machineFile);
		delete process.env.SUTANDO_MEMORY_DIR;
	});

	it('returns workspace/filename when file exists in workspace (no memory dir)', () => {
		delete process.env.SUTANDO_MEMORY_DIR;
		delete process.env.SUTANDO_PRIVATE_DIR;
		const ws = join(TMP, 'machine-ws');
		const wsFile = join(ws, 'existing.json');
		writeFileSync(wsFile, '{}');
		const result = personalPath('existing.json', ws);
		assert.equal(result, wsFile);
	});

	it('returns assets/stand-avatar.png when that file exists in workspace', () => {
		delete process.env.SUTANDO_MEMORY_DIR;
		delete process.env.SUTANDO_PRIVATE_DIR;
		const ws = join(TMP, 'machine-ws');
		const avatarFile = join(ws, 'assets', 'stand-avatar.png');
		writeFileSync(avatarFile, 'fake-png');
		const result = personalPath('stand-avatar.png', ws);
		assert.equal(result, avatarFile);
	});

	it('returns preferred machine path when SUTANDO_MEMORY_DIR set but file absent everywhere', () => {
		process.env.SUTANDO_MEMORY_DIR = join(TMP, 'machine-memory');
		const ws = join(TMP, 'machine-ws');
		const result = personalPath('no-such-file.json', ws);
		// Should prefer the machine-<host> path inside the memory dir
		assert.equal(result, join(TMP, 'machine-memory', `machine-${HOST}`, 'no-such-file.json'));
		delete process.env.SUTANDO_MEMORY_DIR;
	});
});
