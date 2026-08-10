/**
 * The TS and Python resolvers must agree on the home-relative last-ditch workspace.
 *
 * They didn't: TS returned `~/.sutando/workspace` (the retired root) where Python returns
 * `~/sutando-workspace`. Every TS caller of resolveWorkspace() inherits that branch, so a
 * disagreement puts TS services in a different workspace from the Python core.
 *
 * Run: tsx --test tests/workspace-last-ditch-matches-python.test.ts
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir, homedir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { LAST_DITCH_WORKSPACE_REL, findRepoRoot } from '../src/sutando_config.js';

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '..');

/** Python's own answer, from the production helper — not a copy of its constant. */
function pythonLastDitch(): string {
	return execFileSync(
		'python3',
		['-c', "import sys; sys.path.insert(0,'src'); import workspace_default as w; print(w.default_workspace_dir(), end='')"],
		{ cwd: REPO, encoding: 'utf-8' },
	);
}

describe('last-ditch workspace parity between the TS and Python resolvers', () => {
	it('python3 and the helper are reachable — otherwise every assertion below is vacuous', () => {
		const got = pythonLastDitch();
		assert.ok(got.length > 0, 'python helper produced nothing');
		assert.equal(resolve(got), resolve(join(homedir(), basename(got))),
			`expected a HOME-relative single segment, got ${got}`);
	});

	it('TS last-ditch equals what Python resolves to', () => {
		const py = pythonLastDitch();
		assert.equal(
			resolve(join(homedir(), LAST_DITCH_WORKSPACE_REL)),
			resolve(py),
			'TS and Python disagree on the last-ditch workspace',
		);
	});

	it('is not the retired root', () => {
		assert.notEqual(LAST_DITCH_WORKSPACE_REL, '.sutando/workspace');
		assert.ok(
			!resolve(join(homedir(), LAST_DITCH_WORKSPACE_REL)).startsWith(resolve(join(homedir(), '.sutando')) + '/'),
			'last-ditch still lands under the retired ~/.sutando tree',
		);
	});

	it('the branch is reachable, not dead code', () => {
		// findRepoRoot walks up from a start dir; with no sutando.config.json within
		// its hop limit it returns undefined, which is what selects the last-ditch.
		const bare = mkdtempSync(join(tmpdir(), 'sutando-no-config-'));
		try {
			assert.equal(findRepoRoot(bare), undefined,
				'a config-less tree still resolved a repo root — fixture is unrepresentative');
		} finally {
			rmSync(bare, { recursive: true, force: true });
		}
		// Control: the real repo MUST resolve, or the assertion above proves nothing
		// about config detection and would pass on a broken findRepoRoot.
		assert.equal(findRepoRoot(REPO), REPO, 'findRepoRoot failed on the real repo');
	});
});
