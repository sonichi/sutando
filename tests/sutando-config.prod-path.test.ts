/**
 * PR #1440 — production-path tests for src/sutando_config.ts.
 *
 * Mini review noted that the existing test suite leans on `SUTANDO_TEST_MODE=1`,
 * leaving the production code path (env-set, no escape hatch) under-covered. This
 * file exercises that path directly and asserts the B4 safety properties:
 *
 *   - NO_COLOR=1 → no ANSI escapes in deprecation stderr, even on a TTY-like stream.
 *   - Non-TTY stderr → no ANSI escapes regardless of NO_COLOR.
 *   - Warning text contains NO literal `'<value>'` interpolation for either the
 *     env-var value or the .env-declared value (B4 path-leak parity with c58270d).
 *   - Warning fires exactly once per process (resetCacheForTests as the inverse).
 *
 * Run: tsx --test tests/sutando-config.prod-path.test.ts
 */
import { describe, it, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { resetCacheForTests, resolveWorkspace } from '../src/sutando_config.js';

interface CapturedStderr {
	chunks: string[];
	restore: () => void;
}

function captureStderr(): CapturedStderr {
	const chunks: string[] = [];
	const orig = process.stderr.write.bind(process.stderr);
	process.stderr.write = ((chunk: string | Uint8Array, ..._args: unknown[]): boolean => {
		const s = typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8');
		chunks.push(s);
		return true;
	}) as typeof process.stderr.write;
	return {
		chunks,
		restore: () => {
			process.stderr.write = orig;
		},
	};
}

describe('sutando_config — production path (env-set, no TEST_MODE)', () => {
	let savedEnv: string | undefined;
	let savedNoColor: string | undefined;
	let savedTestMode: string | undefined;
	let repo: string;

	beforeEach(() => {
		savedEnv = process.env.SUTANDO_WORKSPACE;
		savedNoColor = process.env.NO_COLOR;
		savedTestMode = process.env.SUTANDO_TEST_MODE;
		delete process.env.SUTANDO_WORKSPACE;
		delete process.env.NO_COLOR;
		delete process.env.SUTANDO_TEST_MODE;
		resetCacheForTests();
		repo = mkdtempSync(join(tmpdir(), 'sutando-prod-path-'));
	});

	afterEach(() => {
		resetCacheForTests();
		if (savedEnv === undefined) delete process.env.SUTANDO_WORKSPACE;
		else process.env.SUTANDO_WORKSPACE = savedEnv;
		if (savedNoColor === undefined) delete process.env.NO_COLOR;
		else process.env.NO_COLOR = savedNoColor;
		if (savedTestMode === undefined) delete process.env.SUTANDO_TEST_MODE;
		else process.env.SUTANDO_TEST_MODE = savedTestMode;
		if (repo) rmSync(repo, { recursive: true, force: true });
	});

	// --- B4: NO_COLOR honored --------------------------------------------- //

	it('NO_COLOR=1 → deprecation warning has zero ANSI escapes', () => {
		process.env.SUTANDO_WORKSPACE = '/from/env';
		process.env.NO_COLOR = '1';
		const cap = captureStderr();
		try {
			resolveWorkspace(repo);
		} finally {
			cap.restore();
		}
		const combined = cap.chunks.join('');
		assert.ok(combined.includes('NO LONGER HONORED'), 'expected deprecation text in stderr');
		// eslint-disable-next-line no-control-regex
		assert.equal(combined.match(/\x1b\[/g), null, `expected zero ANSI escapes, got: ${combined}`);
	});

	it('non-TTY stderr → zero ANSI escapes (no NO_COLOR set)', () => {
		// In node:test, process.stderr.isTTY is undefined/falsy. The code branch
		// `process.stderr.isTTY` short-circuits to plain text. This guards against
		// a regression where the branch defaults to ANSI on falsy isTTY.
		process.env.SUTANDO_WORKSPACE = '/from/env';
		const cap = captureStderr();
		try {
			resolveWorkspace(repo);
		} finally {
			cap.restore();
		}
		const combined = cap.chunks.join('');
		assert.ok(combined.includes('NO LONGER HONORED'), 'expected deprecation text in stderr');
		// eslint-disable-next-line no-control-regex
		assert.equal(combined.match(/\x1b\[/g), null, `expected zero ANSI escapes on non-TTY, got: ${combined}`);
	});

	// --- B4: path-leak — no literal '${envVal}' interpolation -------------- //

	it('warning text does NOT contain the literal env-var path value', () => {
		// c58270d parity (Python) — the deprecation warning must not echo the
		// /-bearing value into stderr because a caller-side `$(... 2>&1)` capture
		// followed by `mkdir -p "$captured"` previously tokenized the / chars
		// into a rogue folder tree. The advice text still mentions
		// `--dry-run`/`--commit` so operators know what to run.
		const sneakyValue = '/this/literal/should/not/appear/in/stderr';
		process.env.SUTANDO_WORKSPACE = sneakyValue;
		const cap = captureStderr();
		try {
			resolveWorkspace(repo);
		} finally {
			cap.restore();
		}
		const combined = cap.chunks.join('');
		assert.ok(
			!combined.includes(sneakyValue),
			`warning leaked the env-var value: ${JSON.stringify(combined)}`,
		);
	});

	it('warning text does NOT contain the literal .env-declared path value', () => {
		// Same B4 property for the .env-drift warning (second stderr line).
		// Note: this requires the .env to declare SUTANDO_WORKSPACE so the
		// dotenv-drift branch fires; we write a .env into the test repo.
		const sneakyDotenv = '/sneaky/dotenv/value/should/not/leak';
		writeFileSync(join(repo, '.env'), `SUTANDO_WORKSPACE=${sneakyDotenv}\n`, 'utf8');
		// SUTANDO_WORKSPACE in the env triggers the first warning; without it
		// the dotenv branch still fires (it's keyed off the file content, not
		// the process env).
		const cap = captureStderr();
		try {
			resolveWorkspace(repo);
		} finally {
			cap.restore();
		}
		const combined = cap.chunks.join('');
		// The first (env) warning may or may not fire; the second (.env-drift)
		// must not leak the literal value.
		assert.ok(
			!combined.includes(sneakyDotenv),
			`.env-drift warning leaked the dotenv value: ${JSON.stringify(combined)}`,
		);
	});

	// --- B4: warning fires exactly once per process ------------------------ //

	it('deprecation warning is one-shot across multiple resolveWorkspace calls', () => {
		process.env.SUTANDO_WORKSPACE = '/from/env';
		const cap = captureStderr();
		try {
			resolveWorkspace(repo);
			resolveWorkspace(repo);
			resolveWorkspace(repo);
		} finally {
			cap.restore();
		}
		const combined = cap.chunks.join('');
		const occurrences = combined.match(/NO LONGER HONORED/g)?.length ?? 0;
		assert.equal(
			occurrences,
			1,
			`expected exactly one deprecation warning across 3 calls, got ${occurrences}`,
		);
	});

	// --- resolver still returns config/default path with env set ----------- //

	it('env set + config has workspace.path → resolver returns the config-driven path', () => {
		writeFileSync(
			join(repo, 'sutando.config.local.json'),
			JSON.stringify({ workspace: { path: '/from/local/config' } }),
			'utf8',
		);
		process.env.SUTANDO_WORKSPACE = '/from/env';
		const cap = captureStderr();
		try {
			const resolved = resolveWorkspace(repo);
			assert.equal(resolved, '/from/local/config');
		} finally {
			cap.restore();
		}
	});

	it('env set + no config → resolver returns the baked-in {repoRoot}/workspace', () => {
		process.env.SUTANDO_WORKSPACE = '/from/env';
		const cap = captureStderr();
		try {
			const resolved = resolveWorkspace(repo);
			assert.equal(resolved, join(repo, 'workspace'));
		} finally {
			cap.restore();
		}
	});
});
