import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

// Regression guard for the presenter-mode sentinel gate (#1171).
//
// Before this fix slideControlTool + fullscreenTool were unconditionally
// included in inlineTools/ownerOnlyTools, so Gemini fired them on greetings
// (3 unprovoked copres_next_anchor in <40s on a "can you hear me?" session).
// The fix gates EXPOSURE at module load on state/presenter-mode.sentinel.

const SRC = readFileSync(
	join(import.meta.dirname ?? '.', '..', 'src/inline-tools.ts'),
	'utf-8',
);

const GATED_SPREAD = /\.\.\.\(_presenterActive\s*\?\s*\[\s*slideControlTool\s*,\s*fullscreenTool\s*\]\s*:\s*\[\s*\]\s*\)/;

function extractBlock(startMarker: string, endMarker: string): string {
	const startIdx = SRC.indexOf(startMarker);
	const endIdx = SRC.indexOf(endMarker, startIdx);
	if (startIdx < 0 || endIdx < 0) return '';
	return SRC.slice(startIdx, endIdx + endMarker.length);
}

const INLINE_TOOLS_BLOCK = extractBlock(
	'export const inlineTools = assertUniqueToolNames(',
	']);',
);
const OWNER_ONLY_BLOCK = extractBlock(
	'export const ownerOnlyTools = [',
	'];',
);

describe('inline-tools — presenter-mode sentinel gate (#1171)', () => {
	it('declares _presenterActive derived from existsSync(...presenter-mode.sentinel)', () => {
		assert.match(
			SRC,
			/_presenterActive\s*=\s*existsSync\(\s*join\(WORKSPACE_DIR,\s*['"]state['"]\s*,\s*['"]presenter-mode\.sentinel['"]\s*\)\s*\)/,
		);
	});

	it('inlineTools contains the _presenterActive-gated spread', () => {
		assert.ok(INLINE_TOOLS_BLOCK);
		assert.match(INLINE_TOOLS_BLOCK, GATED_SPREAD);
	});

	it('ownerOnlyTools contains the _presenterActive-gated spread', () => {
		assert.ok(OWNER_ONLY_BLOCK);
		assert.match(OWNER_ONLY_BLOCK, GATED_SPREAD);
	});

	it('no bare slideControlTool/fullscreenTool outside the gated spread (inlineTools)', () => {
		const stripped = INLINE_TOOLS_BLOCK.replace(GATED_SPREAD, '');
		assert.doesNotMatch(stripped, /slideControlTool|fullscreenTool/);
	});

	it('no bare slideControlTool/fullscreenTool outside the gated spread (ownerOnlyTools)', () => {
		const stripped = OWNER_ONLY_BLOCK.replace(GATED_SPREAD, '');
		assert.doesNotMatch(stripped, /slideControlTool|fullscreenTool/);
	});

	it('#1171 issue reference present in gate comment', () => {
		assert.match(SRC, /#1171/);
	});

	it('_presenterActive declared before inlineTools array literal', () => {
		const presenterIdx = SRC.indexOf('_presenterActive');
		const inlineToolsIdx = SRC.indexOf('export const inlineTools');
		assert.ok(presenterIdx > 0 && inlineToolsIdx > 0);
		assert.ok(presenterIdx < inlineToolsIdx);
	});
});
