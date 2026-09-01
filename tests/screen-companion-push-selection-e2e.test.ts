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
import { setTimeout as delay } from 'node:timers/promises';
import {
	registerSource,
	setVisionSession,
	captureSendFrame,
	resetVisionEgressForTests,
	VISION_MIN_SEND_INTERVAL_MS,
	_getVisionFrameHookCount,
} from '../src/vision-tools.js';

// P7 D7.4: real sends are paced by the egress token bucket (~1 fps) — the
// e2e drives real sends, so it must respect the designed cadence.
async function pacedCapture(name: string): Promise<Awaited<ReturnType<typeof captureSendFrame>>> {
	const r = await captureSendFrame(name);
	await delay(VISION_MIN_SEND_INTERVAL_MS + 50);
	return r;
}
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
	resetVisionEgressForTests();
	_setVisionQueryDeps({ readSelection: () => ({ text: 'HELLO', source: 'ax_selection' }) });
	try {
		// _frameHook probes every 3rd frame — drive 3 PACED captures to hit
		// the boundary (the P7 egress bucket caps real sends at ~1 fps).
		await pacedCapture('testsrc');
		await pacedCapture('testsrc');
		await pacedCapture('testsrc');
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
	resetVisionEgressForTests();
	_setVisionQueryDeps({ readSelection: () => ({ text: 'HELLO', source: 'ax_selection' }) });
	let r;
	try {
		await pacedCapture('testsrc2');
		await pacedCapture('testsrc2');
		r = await pacedCapture('testsrc2'); // probe tick — would inject if sendContent existed
	} finally {
		_resetVisionQueryDeps();
	}

	assert.equal(r?.ok, true, 'capture still succeeds without sendContent');
	assert.deepEqual(frames, ['image/jpeg', 'image/jpeg', 'image/jpeg']);
});
