import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

/**
 * Tests for the `hosts/<host>/` per-host read probe in personalPath (H4 fix).
 * Mirrors tests/util-paths-hosts-resolution.test.py.
 *
 * The per-host relocation (#1717) moves per-host files into
 * `<workspace>/hosts/<hostname>/`. personalPath() must probe that location
 * FIRST so relocated files are found; when absent, resolution must be
 * identical to the pre-#1717 order (purely additive, no regression).
 */

import { personalPath } from '../src/util_paths.js';

function clearEnv() {
	delete process.env.SUTANDO_MEMORY_DIR;
	delete process.env.SUTANDO_PRIVATE_DIR;
	delete process.env.SUTANDO_HOST_LABEL;
}

describe('personalPath hosts/<host>/ resolution (#1717 / H4)', () => {
	it('returns hosts/<host>/ copy ahead of the workspace-root copy', () => {
		clearEnv();
		process.env.SUTANDO_HOST_LABEL = 'test-host';
		const ws = mkdtempSync(join(tmpdir(), 'sut-hosts-'));
		try {
			const hostDir = join(ws, 'hosts', 'test-host');
			mkdirSync(hostDir, { recursive: true });
			writeFileSync(join(hostDir, 'stand-identity.json'), '{}');
			writeFileSync(join(ws, 'stand-identity.json'), '{}'); // stale root copy
			const p = personalPath('stand-identity.json', ws);
			assert.equal(p, join(hostDir, 'stand-identity.json'));
		} finally {
			rmSync(ws, { recursive: true, force: true });
			clearEnv();
		}
	});

	it('falls back to workspace root when no hosts/ file exists', () => {
		clearEnv();
		process.env.SUTANDO_HOST_LABEL = 'test-host';
		const ws = mkdtempSync(join(tmpdir(), 'sut-hosts-'));
		try {
			writeFileSync(join(ws, 'pending-questions.md'), 'q');
			const p = personalPath('pending-questions.md', ws);
			assert.equal(p, join(ws, 'pending-questions.md'));
		} finally {
			rmSync(ws, { recursive: true, force: true });
			clearEnv();
		}
	});

	it('nothing-exists preferred return is workspace root (write target untouched)', () => {
		clearEnv();
		process.env.SUTANDO_HOST_LABEL = 'test-host';
		const ws = mkdtempSync(join(tmpdir(), 'sut-hosts-'));
		try {
			const p = personalPath('never-created.json', ws);
			assert.equal(p, join(ws, 'never-created.json'));
		} finally {
			rmSync(ws, { recursive: true, force: true });
			clearEnv();
		}
	});

	it('hosts/<host>/ wins over legacy machine-<host>/', () => {
		clearEnv();
		process.env.SUTANDO_HOST_LABEL = 'test-host';
		const ws = mkdtempSync(join(tmpdir(), 'sut-hosts-'));
		const mem = mkdtempSync(join(tmpdir(), 'sut-mem-'));
		try {
			const machineDir = join(mem, 'machine-test-host');
			mkdirSync(machineDir, { recursive: true });
			writeFileSync(join(machineDir, 'f.json'), 'legacy');
			const hostDir = join(ws, 'hosts', 'test-host');
			mkdirSync(hostDir, { recursive: true });
			writeFileSync(join(hostDir, 'f.json'), 'new');
			process.env.SUTANDO_MEMORY_DIR = mem;
			const p = personalPath('f.json', ws);
			assert.equal(p, join(hostDir, 'f.json'));
		} finally {
			rmSync(ws, { recursive: true, force: true });
			rmSync(mem, { recursive: true, force: true });
			clearEnv();
		}
	});

	it('legacy machine-<host>/ still found when hosts/ absent', () => {
		clearEnv();
		process.env.SUTANDO_HOST_LABEL = 'test-host';
		const ws = mkdtempSync(join(tmpdir(), 'sut-hosts-'));
		const mem = mkdtempSync(join(tmpdir(), 'sut-mem-'));
		try {
			const machineDir = join(mem, 'machine-test-host');
			mkdirSync(machineDir, { recursive: true });
			writeFileSync(join(machineDir, 'g.json'), 'legacy');
			process.env.SUTANDO_MEMORY_DIR = mem;
			const p = personalPath('g.json', ws);
			assert.equal(p, join(machineDir, 'g.json'));
		} finally {
			rmSync(ws, { recursive: true, force: true });
			rmSync(mem, { recursive: true, force: true });
			clearEnv();
		}
	});

	it('uses a dotted SUTANDO_HOST_LABEL raw (not split) — parity with PY', () => {
		// Mini #1718 review note 1: an explicit label is an override, used
		// verbatim. A dotted label must resolve hosts/a.b/, not hosts/a/.
		clearEnv();
		process.env.SUTANDO_HOST_LABEL = 'a.b';
		const ws = mkdtempSync(join(tmpdir(), 'sut-hosts-'));
		try {
			const hostDir = join(ws, 'hosts', 'a.b');
			mkdirSync(hostDir, { recursive: true });
			writeFileSync(join(hostDir, 'f.json'), 'x');
			const p = personalPath('f.json', ws);
			assert.equal(p, join(hostDir, 'f.json'));
		} finally {
			rmSync(ws, { recursive: true, force: true });
			clearEnv();
		}
	});
});
