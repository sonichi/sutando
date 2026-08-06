// The sibling test (screen-companion-push-selection.test.ts) exercises
// `_frameHook` in isolation. Neither it nor anything else verifies the wiring
// this PR actually depends on: that vision-tools' `captureAndSend()` FIRES the
// screen-companion hook AFTER sending the frame, with a working `sendUserCtx`
// bound to `transport.sendContent`. This test drives the real capture path
// (`captureSendFrame` → `captureAndSend`) with the real registered hook and a
// fake session, asserting a real call delivers the selected text to the model
// in the right order (frame first, then selection). Only the unavoidable AX
// layer (`readSelection`) is stubbed.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
	registerSource,
	setVisionSession,
	captureSendFrame,
	_getVisionFrameHookCount,
} from '../src/vision-tools.js';
// Importing the skill registers `_frameHook` into vision-tools' hook registry
// at module-load time (registerVisionFrameHook(_frameHook)).
import {
	_setVisionQueryDeps,
	_resetVisionQueryDeps,
	_resetFrameHookState,
} from '../skills/screen-companion/tools.js';

test('captureAndSend fires the screen-companion hook and injects selection after the frame', async () => {
	assert.ok(
		_getVisionFrameHookCount() >= 1,
		'screen-companion hook should be registered on import',
	);

	const events: string[] = [];
	setVisionSession({
		transport: {
			sendFile: (_b64: string, mime: string) => events.push(`frame:${mime}`),
			sendContent: (turns: Array<{ role: string; text: string }>) =>
				events.push(`inject:${turns[0].text}`),
		},
	});
	registerSource({
		name: 'testsrc',
		async capture() {
			return { data: Buffer.from('x'), mimeType: 'image/jpeg' };
		},
	});

	_resetFrameHookState();
	_setVisionQueryDeps({ readSelection: () => ({ text: 'HELLO', source: 'ax_selection' }) });
	try {
		// _frameHook probes every 3rd frame — drive 3 captures to hit the boundary.
		await captureSendFrame('testsrc');
		await captureSendFrame('testsrc');
		await captureSendFrame('testsrc');
	} finally {
		_resetVisionQueryDeps();
	}

	// Three frames sent; the selection is injected exactly once, and only AFTER
	// the third frame — proving the hook is fired by captureAndSend post-send
	// and its sendUserCtx reaches transport.sendContent.
	assert.deepEqual(events, [
		'frame:image/jpeg',
		'frame:image/jpeg',
		'frame:image/jpeg',
		'inject:[Selected text: HELLO]',
	]);
});

test('no injection when the session transport has no sendContent (graceful)', async () => {
	const frames: string[] = [];
	setVisionSession({
		transport: {
			sendFile: (_b64: string, mime: string) => frames.push(mime),
			// no sendContent — hook must not throw the capture path
		},
	});
	registerSource({
		name: 'testsrc2',
		async capture() {
			return { data: Buffer.from('x'), mimeType: 'image/jpeg' };
		},
	});

	_resetFrameHookState();
	_setVisionQueryDeps({ readSelection: () => ({ text: 'HELLO', source: 'ax_selection' }) });
	let r;
	try {
		await captureSendFrame('testsrc2');
		await captureSendFrame('testsrc2');
		r = await captureSendFrame('testsrc2'); // probe tick — would inject if sendContent existed
	} finally {
		_resetVisionQueryDeps();
	}

	assert.equal(r?.ok, true, 'capture still succeeds without sendContent');
	assert.deepEqual(frames, ['image/jpeg', 'image/jpeg', 'image/jpeg']);
});
