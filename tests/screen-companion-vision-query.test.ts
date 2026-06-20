// #1389 / PR #1409 — vision_query mode handling.
//
// Confirms the return-shape contract for the four modes (mode is REQUIRED — no default):
//   - mode='selection' with selection present          → selection_read, no frame capture
//   - mode='selection' with no selection               → no_selection, no frame capture
//   - mode='frame'                                      → frame_captured, no selection probe
//   - mode='selection-or-frame' with selection present → selection_read, no frame capture
//   - mode='selection-or-frame' with no selection      → frame_captured (fall-through)
//   - mode='both' with selection present               → selection_and_frame
//   - mode='both' with no selection                    → frame_captured (fall-through)
//
// Uses the _setVisionQueryDeps test seam to stub the readSelection +
// captureSendFrame side-effects — keeps the test hermetic on machines without
// AX permission or a live Gemini session.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { tools as scTools, _setVisionQueryDeps, _resetVisionQueryDeps } from '../skills/screen-companion/tools.js';
import type { SelectionResult } from '../skills/screen-companion/scripts/read-selection.js';

const visionQuery = scTools.find(t => t.name === 'vision_query');
assert.ok(visionQuery, 'vision_query tool must be exported');

// vision_query doesn't use the ctx; pass a minimal stub.
const ctx = {} as any;

function withDeps(
	deps: {
		readSelection?: () => SelectionResult | null;
		captureSendFrame?: () => Promise<{ ok: boolean; source?: string; error?: string }>;
	},
	fn: () => Promise<void>,
): () => Promise<void> {
	return async () => {
		_setVisionQueryDeps(deps as any);
		try {
			await fn();
		} finally {
			_resetVisionQueryDeps();
		}
	};
}

test(
	'mode="selection" with selection present → selection_read, no frame capture',
	withDeps(
		{
			readSelection: () => ({ text: 'hello world', source: 'ax_selection' }),
			captureSendFrame: async () => {
				throw new Error('frame capture should NOT run in mode=selection');
			},
		},
		async () => {
			const r = (await visionQuery!.execute({ mode: 'selection', question: 'what does this say?' } as any, ctx)) as any;
			assert.equal(r.status, 'selection_read');
			assert.equal(r.text, 'hello world');
			assert.equal(r.source, 'ax_selection');
			assert.equal(r.question, 'what does this say?');
		},
	),
);

test(
	'mode="selection" with no selection → no_selection, no frame capture',
	withDeps(
		{
			readSelection: () => null,
			captureSendFrame: async () => {
				throw new Error('frame capture should NOT run in mode=selection');
			},
		},
		async () => {
			const r = (await visionQuery!.execute({ mode: 'selection' } as any, ctx)) as any;
			assert.equal(r.status, 'no_selection');
		},
	),
);

test(
	'mode="frame" → captures frame, no selection probe',
	withDeps(
		{
			readSelection: () => {
				throw new Error('selection probe should NOT run in mode=frame');
			},
			captureSendFrame: async () => ({ ok: true, source: 'screen' }),
		},
		async () => {
			const r = (await visionQuery!.execute({ mode: 'frame', question: 'is the dialog open?' } as any, ctx)) as any;
			assert.equal(r.status, 'frame_captured');
			assert.equal(r.source, 'screen');
			assert.equal(r.question, 'is the dialog open?');
		},
	),
);

test(
	'mode="selection-or-frame" with selection present → selection_read, no frame capture',
	withDeps(
		{
			readSelection: () => ({ text: 'highlighted phrase', source: 'ax_selection' }),
			captureSendFrame: async () => {
				throw new Error('frame capture should NOT run when selection-or-frame has a selection');
			},
		},
		async () => {
			const r = (await visionQuery!.execute({ mode: 'selection-or-frame', question: 'what does this say?' } as any, ctx)) as any;
			assert.equal(r.status, 'selection_read');
			assert.equal(r.text, 'highlighted phrase');
			assert.equal(r.source, 'ax_selection');
			assert.equal(r.question, 'what does this say?');
		},
	),
);

test(
	'mode="selection-or-frame" with no selection → falls through to frame_captured',
	withDeps(
		{
			readSelection: () => null,
			captureSendFrame: async () => ({ ok: true, source: 'screen' }),
		},
		async () => {
			const r = (await visionQuery!.execute({ mode: 'selection-or-frame' } as any, ctx)) as any;
			assert.equal(r.status, 'frame_captured');
			assert.equal(r.source, 'screen');
		},
	),
);

test(
	'mode="both" with selection present → selection_and_frame',
	withDeps(
		{
			readSelection: () => ({ text: 'quoted sentence', source: 'chrome_js_selection' }),
			captureSendFrame: async () => ({ ok: true, source: 'screen' }),
		},
		async () => {
			const r = (await visionQuery!.execute({ mode: 'both', question: 'what is this?' } as any, ctx)) as any;
			assert.equal(r.status, 'selection_and_frame');
			assert.deepEqual(r.selection, { text: 'quoted sentence', source: 'chrome_js_selection' });
			assert.equal(r.frame_status, 'ok');
			assert.equal(r.frame_source, 'screen');
		},
	),
);

test(
	'mode="both" with no selection → falls through to frame_captured',
	withDeps(
		{
			readSelection: () => null,
			captureSendFrame: async () => ({ ok: true, source: 'screen' }),
		},
		async () => {
			const r = (await visionQuery!.execute({ mode: 'both' } as any, ctx)) as any;
			assert.equal(r.status, 'frame_captured');
			assert.equal(r.source, 'screen');
		},
	),
);

test(
	'mode="frame" with capture failure → failed with hint',
	withDeps(
		{
			readSelection: () => {
				throw new Error('selection probe should NOT run in mode=frame');
			},
			captureSendFrame: async () => ({ ok: false, error: 'no session' }),
		},
		async () => {
			const r = (await visionQuery!.execute({ mode: 'frame' } as any, ctx)) as any;
			assert.equal(r.status, 'failed');
			assert.equal(r.error, 'no session');
			assert.ok(r.hint && typeof r.hint === 'string');
		},
	),
);
