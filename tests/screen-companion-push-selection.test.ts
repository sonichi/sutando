// Verifies that the VisionFramePostSendHook registered by screen-companion:
//   - fires sendUserCtx only on SELECTION_PROBE_INTERVAL_TICKS boundaries
//   - injects "[Selected text: ...]" when selection appears
//   - does NOT inject again when selection hasn't changed
//   - injects again when selection changes to a new value
//   - does NOT inject when there is no selection
//
// Uses _setVisionQueryDeps to stub readSelection and _resetFrameHookState to
// isolate tick-counter + last-selection state between tests.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
	_frameHook,
	_resetFrameHookState,
	_setVisionQueryDeps,
	_resetVisionQueryDeps,
} from '../skills/screen-companion/tools.js';
import type { SelectionResult } from '../skills/screen-companion/scripts/read-selection.js';

function makeCtx() {
	const calls: string[] = [];
	const sendUserCtx = (text: string) => calls.push(text);
	return { calls, sendUserCtx };
}

async function withSelection(
	result: SelectionResult | null,
	fn: () => void,
): Promise<void> {
	_setVisionQueryDeps({ readSelection: () => result });
	try {
		fn();
		// P7: the hook's probe is fire-and-forget async — flush the microtask
		// queue so injections land before the assertions run.
		await new Promise((r) => setImmediate(r));
	} finally {
		_resetVisionQueryDeps();
	}
}

// Each test resets module state first.

test('does not call sendUserCtx on ticks 1 and 2 (below interval)', async () => {
	_resetFrameHookState();
	const { calls, sendUserCtx } = makeCtx();
	await withSelection({ text: 'hello', source: 'ax_selection' }, () => {
		_frameHook(sendUserCtx); // tick 1
		_frameHook(sendUserCtx); // tick 2
	});
	assert.deepEqual(calls, []);
});

test('injects selection on tick 3 when text is present', async () => {
	_resetFrameHookState();
	const { calls, sendUserCtx } = makeCtx();
	await withSelection({ text: 'hello', source: 'ax_selection' }, () => {
		_frameHook(sendUserCtx); // 1
		_frameHook(sendUserCtx); // 2
		_frameHook(sendUserCtx); // 3 — probe fires
	});
	assert.deepEqual(calls, ['[Selected text: hello]']);
});

test('does NOT inject again when selection unchanged on next interval', async () => {
	_resetFrameHookState();
	const { calls, sendUserCtx } = makeCtx();
	await withSelection({ text: 'hello', source: 'ax_selection' }, () => {
		for (let i = 0; i < 6; i++) _frameHook(sendUserCtx); // ticks 1–6 (probes at 3, 6)
	});
	// Both probes return 'hello'; should inject only once (first change: '' → 'hello')
	assert.deepEqual(calls, ['[Selected text: hello]']);
});

test('injects again when selection text changes', async () => {
	_resetFrameHookState();
	const { calls, sendUserCtx } = makeCtx();
	// First probe: 'hello'
	await withSelection({ text: 'hello', source: 'ax_selection' }, () => {
		for (let i = 0; i < 3; i++) _frameHook(sendUserCtx);
	});
	// Second probe interval: 'world'
	await withSelection({ text: 'world', source: 'ax_selection' }, () => {
		for (let i = 0; i < 3; i++) _frameHook(sendUserCtx);
	});
	assert.deepEqual(calls, ['[Selected text: hello]', '[Selected text: world]']);
});

test('does not inject when no selection is found', async () => {
	_resetFrameHookState();
	const { calls, sendUserCtx } = makeCtx();
	await withSelection(null, () => {
		for (let i = 0; i < 3; i++) _frameHook(sendUserCtx); // probe fires on tick 3, returns null
	});
	assert.deepEqual(calls, []);
});
