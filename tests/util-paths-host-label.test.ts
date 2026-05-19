import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { hostname } from 'node:os';

/**
 * Tests for SUTANDO_HOST_LABEL (#871) in src/util_paths.ts. Twin of
 * tests/util-paths-host-label.test.py.
 *
 * Covers:
 *   1. Unset → falls back to hostname.
 *   2. Set → override returned (trimmed).
 *   3. Empty / whitespace-only → treated as unset (don't collapse to
 *      `machine-/`).
 *   4. `personalPath()` composes `machine-<label>/` with the override.
 */

import { hostLabel, personalPath } from '../src/util_paths.js';

function clearEnv() {
	delete process.env.SUTANDO_MEMORY_DIR;
	delete process.env.SUTANDO_PRIVATE_DIR;
	delete process.env.SUTANDO_HOST_LABEL;
}

describe('hostLabel (#871 override)', () => {
	it('falls back to the system hostname when unset', () => {
		clearEnv();
		assert.equal(hostLabel(), hostname().split('.')[0]);
	});

	it('returns the override verbatim', () => {
		clearEnv();
		process.env.SUTANDO_HOST_LABEL = 'my-stable-mac';
		assert.equal(hostLabel(), 'my-stable-mac');
		clearEnv();
	});

	it('trims whitespace from the override', () => {
		clearEnv();
		process.env.SUTANDO_HOST_LABEL = '  studio-2  ';
		assert.equal(hostLabel(), 'studio-2');
		clearEnv();
	});

	it('falls back to hostname on empty override', () => {
		// Regression guard: empty env var must not produce `machine-/`.
		clearEnv();
		process.env.SUTANDO_HOST_LABEL = '';
		assert.equal(hostLabel(), hostname().split('.')[0]);
		clearEnv();
	});

	it('falls back to hostname on whitespace-only override', () => {
		clearEnv();
		process.env.SUTANDO_HOST_LABEL = '   ';
		assert.equal(hostLabel(), hostname().split('.')[0]);
		clearEnv();
	});

	it('personalPath composes machine-<label>/ with the override', () => {
		clearEnv();
		process.env.SUTANDO_MEMORY_DIR = '/tmp/mem';
		process.env.SUTANDO_HOST_LABEL = 'pinned';
		// Path doesn't exist on disk, so personalPath returns the preferred
		// memory-dir path for the caller's existsSync() check.
		const p = personalPath('stand-identity.json', '/tmp/ws');
		assert.equal(p, '/tmp/mem/machine-pinned/stand-identity.json');
		clearEnv();
	});
});
