import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { inlineTools } from '../src/inline-tools.js';

// Regression guard for the skill-loader deduplication fix.
// When the same skill directory name appears in both the repo skills/ dir and
// the workspace skills/ dir, the last-write-wins Map strategy must prevent
// duplicate tool names from reaching assertUniqueToolNames (which throws on
// dups and kills the voice session at startup — see inline-tools.ts:948).

describe('skill-loader deduplication', () => {
	const src = readFileSync('src/inline-tools.ts', 'utf8');

	it('uses Map-based dedup (ownerBySkill) instead of plain push', () => {
		assert.ok(
			src.includes('ownerBySkill'),
			'loadSkillManifestTools must use ownerBySkill Map for last-write-wins dedup',
		);
	});

	it('uses Map .set() to register tools (not .push)', () => {
		assert.ok(
			src.includes('ownerBySkill).set(dirName'),
			'tools must be registered via .set(dirName) so same-name dirs overwrite rather than append',
		);
	});

	it('flattens Maps to arrays before returning', () => {
		assert.ok(
			src.includes('Array.from(ownerBySkill.values()).flat()'),
			'owner tools must be flattened from Map values',
		);
	});

	it('inlineTools has no duplicate tool names after loading workspace + repo skills', () => {
		const names = inlineTools.map(t => t.name);
		const seen = new Set<string>();
		const dupes: string[] = [];
		for (const n of names) {
			if (seen.has(n)) dupes.push(n);
			seen.add(n);
		}
		assert.deepEqual(dupes, [], `duplicate tool names in inlineTools: ${dupes.join(', ')}`);
	});

	it('inlineTools is a non-empty array', () => {
		assert.ok(Array.isArray(inlineTools) && inlineTools.length > 0, 'inlineTools must be non-empty');
	});
});
