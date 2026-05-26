import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

// Regression guard for PR #1081: inline-tool loader must scan
// $SUTANDO_WORKSPACE/skills/ in addition to REPO_ROOT/skills/ and
// the private memory-dir skills/. The workspace slot (index 1) sits
// between repo (index 0) and private/memory-dir (index 2+), so
// workspace skills can override repo defaults but be overridden by
// private ones (last-write-wins).

const SRC = readFileSync(
	join(import.meta.dirname ?? '.', '..', 'src/inline-tools.ts'),
	'utf-8',
);

describe('inline-tool loader — workspace skills scanning', () => {
	it('loadSkillManifestTools dirsToScan includes WORKSPACE_DIR/skills', () => {
		// The dirsToScan initializer in loadSkillManifestTools must include
		// join(WORKSPACE_DIR, 'skills') alongside join(REPO_ROOT, 'skills').
		assert.match(
			SRC,
			/dirsToScan.*=.*\[.*join\(REPO_ROOT,\s*'skills'\).*,.*join\(WORKSPACE_DIR,\s*'skills'\)/s,
			'loadSkillManifestTools dirsToScan must include both REPO_ROOT and WORKSPACE_DIR skills dirs',
		);
	});

	it('loadCoreDocumentedSkills dirsToScan includes WORKSPACE_DIR/skills', () => {
		// loadCoreDocumentedSkills must mirror the same scan order.
		// Count occurrences of the workspace skills join to ensure both
		// functions have it (loadSkillManifestTools + loadCoreDocumentedSkills).
		const matches = SRC.match(/join\(WORKSPACE_DIR,\s*'skills'\)/g) ?? [];
		assert.ok(
			matches.length >= 2,
			`Expected WORKSPACE_DIR/skills in at least 2 dirsToScan arrays, found ${matches.length}`,
		);
	});

	it('scan order is repo → workspace → private (last-write-wins)', () => {
		// The comment must document "public first, then workspace, then private"
		// order so future readers understand precedence.
		assert.match(
			SRC,
			/public first.*workspace.*private|Order.*public.*workspace.*private/is,
			'Comment must document repo → workspace → private scan order',
		);
	});
});
