/**
 * Regression guard for the "path with a space" bug class.
 *
 * The desktop-bundled install runs from `~/Library/Application Support/…`,
 * which contains a space. Every dev checkout (`~/Documents/github/…`) does not.
 * That asymmetry means this class passes review and CI, then silently fails
 * only for real users. It has now bitten three times:
 *
 *   1. session-recap slug builder                       (#2200)
 *   2. sidecar-supervisor entrypoint guard              (ag2space-cinny-desktop#182)
 *   3. emit-call-tiers entrypoint guard + voice-context REPO_DIR  (this change)
 *
 * The failure signature is always the same and always looks harmless: the
 * process exits 0 with no output, because a guard silently evaluated false.
 *
 * These tests encode the two safe idioms so a regression is caught here rather
 * than by a user noticing a feature never ran.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { realpathSync, mkdtempSync, mkdirSync, writeFileSync, symlinkSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

/** The guard idiom used by emit-call-tiers.ts and sidecar-supervisor.mjs. */
const isEntrypoint = (a: string, b: string): boolean => {
	try {
		return realpathSync(a) === realpathSync(b);
	} catch {
		return false;
	}
};

test('entrypoint guard matches when the path contains a space', () => {
	const dir = mkdtempSync(join(tmpdir(), 'sutando-spaces-'));
	const withSpace = join(dir, 'Application Support');
	const script = join(withSpace, 'entry.mjs');
	try {
		mkdirSync(withSpace, { recursive: true });
		writeFileSync(script, '// entry\n');

		// What node would hand each side: an encoded URL, and a raw argv path.
		const metaUrl = pathToFileURL(script).href;
		assert.ok(metaUrl.includes('%20'), 'precondition: the URL is percent-encoded');

		// The OLD idiom compares an encoded URL against a raw path — never equal.
		assert.notEqual(metaUrl, `file://${script}`, 'the bug: encoded URL !== raw path template');

		// The NEW idiom compares real paths and matches.
		assert.equal(isEntrypoint(fileURLToPath(metaUrl), script), true);
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
});

test('entrypoint guard matches through a symlink', () => {
	const dir = mkdtempSync(join(tmpdir(), 'sutando-spaces-'));
	const script = join(dir, 'entry.mjs');
	const link = join(dir, 'link.mjs');
	try {
		writeFileSync(script, '// entry\n');
		symlinkSync(script, link);
		// node resolves the specifier through symlinks; argv[1] keeps what the
		// caller typed. Only realpath-on-both-sides bridges that.
		assert.equal(isEntrypoint(fileURLToPath(pathToFileURL(script).href), link), true);
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
});

test('entrypoint guard refuses to run on a bare import (no argv script)', () => {
	// realpathSync('') throws; mapping that to false is what keeps `import()`
	// from booting a supervisor and spawning a real core.
	assert.equal(isEntrypoint(fileURLToPath(import.meta.url), ''), false);
});

test('URL.pathname stays percent-encoded — use fileURLToPath for filesystem paths', () => {
	const dir = mkdtempSync(join(tmpdir(), 'sutando-spaces-'));
	const withSpace = join(dir, 'Application Support');
	try {
		mkdirSync(withSpace, { recursive: true });
		const url = pathToFileURL(join(withSpace, 'x.ts'));

		// The bug: .pathname keeps %20, so any join()/readFileSync on it misses.
		assert.ok(url.pathname.includes('%20'));

		// The fix: fileURLToPath decodes back to a real filesystem path.
		assert.ok(!fileURLToPath(url).includes('%20'));
		assert.ok(fileURLToPath(url).includes('Application Support'));
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
});
