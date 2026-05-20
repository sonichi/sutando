import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

/**
 * Tests for the SUTANDO_PRIVATE_DIR -> SUTANDO_MEMORY_DIR rename (#870) in
 * src/util_paths.ts. Mirrors tests/util-paths-memory-dir.test.py.
 *
 * Covers:
 *   1. SUTANDO_MEMORY_DIR is the canonical name.
 *   2. SUTANDO_PRIVATE_DIR is honored as a legacy fallback.
 *   3. SUTANDO_MEMORY_DIR wins when both are set.
 *   4. A deprecation warning is emitted on every read of the legacy alias
 *      (cron / launchd environments miss startup-only warnings).
 *   5. Neither set -> undefined (no warning).
 */

import { memoryDirEnv } from '../src/util_paths.js';

function clearEnv() {
	delete process.env.SUTANDO_MEMORY_DIR;
	delete process.env.SUTANDO_PRIVATE_DIR;
}

function captureWarn<T>(fn: () => T): { value: T; warnings: string[] } {
	const warnings: string[] = [];
	const orig = console.warn;
	console.warn = (...args: unknown[]) => {
		warnings.push(args.map(String).join(' '));
	};
	try {
		const value = fn();
		return { value, warnings };
	} finally {
		console.warn = orig;
	}
}

describe('memoryDirEnv (#870 rename)', () => {
	it('returns undefined and emits no warning when neither var is set', () => {
		clearEnv();
		const { value, warnings } = captureWarn(memoryDirEnv);
		assert.equal(value, undefined);
		assert.deepEqual(warnings, []);
	});

	it('returns SUTANDO_MEMORY_DIR with no warning', () => {
		clearEnv();
		process.env.SUTANDO_MEMORY_DIR = '/tmp/new-memory';
		const { value, warnings } = captureWarn(memoryDirEnv);
		assert.equal(value, '/tmp/new-memory');
		assert.deepEqual(warnings, []);
		clearEnv();
	});

	it('falls back to SUTANDO_PRIVATE_DIR with a deprecation warning', () => {
		clearEnv();
		process.env.SUTANDO_PRIVATE_DIR = '/tmp/legacy-private';
		const { value, warnings } = captureWarn(memoryDirEnv);
		assert.equal(value, '/tmp/legacy-private');
		assert.equal(warnings.length, 1);
		assert.match(warnings[0], /DEPRECATION/);
		assert.match(warnings[0], /SUTANDO_PRIVATE_DIR/);
		assert.match(warnings[0], /SUTANDO_MEMORY_DIR/);
		clearEnv();
	});

	it('prefers SUTANDO_MEMORY_DIR when both are set (no warning)', () => {
		clearEnv();
		process.env.SUTANDO_MEMORY_DIR = '/tmp/new';
		process.env.SUTANDO_PRIVATE_DIR = '/tmp/legacy';
		const { value, warnings } = captureWarn(memoryDirEnv);
		assert.equal(value, '/tmp/new');
		assert.deepEqual(warnings, []);
		clearEnv();
	});

	it('emits the deprecation warning on EVERY read, not just first', () => {
		// Regression guard: cron / launchd environments miss startup-only
		// warnings, so the alias must warn loudly every time it's resolved.
		clearEnv();
		process.env.SUTANDO_PRIVATE_DIR = '/tmp/legacy';
		const { warnings } = captureWarn(() => {
			memoryDirEnv();
			memoryDirEnv();
			memoryDirEnv();
		});
		assert.equal(warnings.length, 3);
		for (const w of warnings) assert.match(w, /DEPRECATION/);
		clearEnv();
	});
});
