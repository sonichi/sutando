import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

// Regression guard for the presenter-mode sentinel gate introduced in #1171.
//
// Before this fix, `slideControlTool` was unconditionally included in
// `inlineTools` and `ownerOnlyTools`. Gemini saw its description ("next slide",
// "previous slide", "go back") on every session turn — including greetings —
// and fired spurious `tool_call` rows in voice sqlite (3 unprovoked
// `copres_next_anchor` calls observed in <40s on a "can you hear me?" session).
//
// The fix gates EXPOSURE at module load: if `state/presenter-mode.sentinel`
// is absent, `slideControlTool` is excluded from the tools arrays Gemini sees.
// Gemini can't fire what it doesn't know about.

const SRC = readFileSync(
	join(import.meta.dirname ?? '.', '..', 'src/inline-tools.ts'),
	'utf-8',
);

describe('inline-tools — presenter-mode sentinel gate (#1171)', () => {
	it('exports presenterModeActive derived from PRESENTER_SENTINEL existsSync', () => {
		assert.match(
			SRC,
			/export const presenterModeActive\s*=\s*existsSync\(PRESENTER_SENTINEL\)/,
			'src/inline-tools.ts must export `presenterModeActive = existsSync(PRESENTER_SENTINEL)`.',
		);
	});

	it('PRESENTER_SENTINEL points to state/presenter-mode.sentinel under WORKSPACE_DIR', () => {
		assert.match(
			SRC,
			/PRESENTER_SENTINEL\s*=\s*join\(WORKSPACE_DIR,\s*['"]state['"]\s*,\s*['"]presenter-mode\.sentinel['"]\)/,
			'PRESENTER_SENTINEL must be join(WORKSPACE_DIR, "state", "presenter-mode.sentinel").',
		);
	});

	it('slideControlTool is conditionally spread into inlineTools based on presenterModeActive', () => {
		assert.match(
			SRC,
			/\.\.\.\(presenterModeActive\s*\?\s*\[slideControlTool\]\s*:\s*\[\]\)/,
			'slideControlTool must be spread conditionally: ...(presenterModeActive ? [slideControlTool] : []).',
		);
	});

	it('slideControlTool is NOT unconditionally listed in inlineTools or ownerOnlyTools', () => {
		// The old pattern was a bare `slideControlTool,` inside the array literal.
		// After the fix both arrays use the spread form — a bare reference must not exist.
		const toolArrayBlock = SRC.match(/export const inlineTools[\s\S]+?export const anyCallerTools/)?.[0] ?? '';
		const ownerBlock = SRC.match(/export const ownerOnlyTools[\s\S]+?export const configurableTools/)?.[0] ?? '';
		const combined = toolArrayBlock + ownerBlock;
		assert.doesNotMatch(
			combined,
			/(?<!\[)slideControlTool(?!\s*\])/,
			'slideControlTool must not appear bare (without conditional spread) in inlineTools or ownerOnlyTools.',
		);
	});

	it('presenterModeActive gate comment references #1171', () => {
		assert.match(
			SRC,
			/#1171/,
			'src/inline-tools.ts must reference issue #1171 in the gate comment.',
		);
	});

	it('presenterModeActive declaration precedes slideControlTool definition', () => {
		const presenterIdx = SRC.indexOf('export const presenterModeActive');
		const slideCtrlIdx = SRC.indexOf('export const slideControlTool');
		assert.ok(
			presenterIdx !== -1 && slideCtrlIdx !== -1,
			'both presenterModeActive and slideControlTool must be present in the source',
		);
		assert.ok(
			presenterIdx < slideCtrlIdx,
			'presenterModeActive must be declared before slideControlTool so the gate reads the sentinel before the tool is defined.',
		);
	});
});
