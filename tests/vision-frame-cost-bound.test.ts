// Frames share one websocket with realtime audio, so an unbounded frame delays
// speech rather than only vision. `boundFrameCost` is the single choke point both
// send paths (pull `captureAndSend`, push `submitFrame`) go through.
//
// These cases are hermetic — they pin the POLICY (passthrough threshold, fail-open
// on a frame sips cannot read, no growth) without needing a display, screen-recording
// permission, or a real capture. The actual Retina downscale ratio is measured
// separately and pasted as before/after evidence in the PR body; a synthetic buffer
// cannot stand in for that number.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { boundFrameCost } from '../src/vision-tools.js';

const KB = 1024;

test('a small frame is passed through byte-identical — a well-behaved producer pays nothing', async () => {
	const small = Buffer.alloc(4 * KB, 0x7f);
	const out = await boundFrameCost(small, 'image/jpeg');
	assert.equal(out.data.byteLength, small.byteLength);
	assert.ok(out.data.equals(small), 'passthrough must not re-encode');
	assert.equal(out.mimeType, 'image/jpeg');
});

test('a frame exactly at the threshold is still passthrough (boundary is inclusive)', async () => {
	const atLimit = Buffer.alloc(200 * KB, 0x41);
	const out = await boundFrameCost(atLimit, 'image/jpeg');
	assert.ok(out.data.equals(atLimit));
});

test('an oversized frame sips cannot decode falls open to the original, never to nothing', async () => {
	// Random bytes over the threshold: sips will reject it. The frame must still
	// reach the session — dropping the user's frame is worse than sending a big one.
	const junk = Buffer.alloc(300 * KB);
	for (let i = 0; i < junk.length; i++) junk[i] = (i * 31) & 0xff;
	const out = await boundFrameCost(junk, 'image/jpeg');
	assert.equal(out.data.byteLength, junk.byteLength, 'fail-open returns the original frame');
	assert.equal(out.mimeType, 'image/jpeg');
});

test('bounding never returns a frame larger than it received', async () => {
	// The guard that makes the fix safe to leave on by default: whatever sips
	// produces, we only adopt it when it is genuinely smaller.
	for (const size of [1 * KB, 200 * KB, 300 * KB]) {
		const buf = Buffer.alloc(size, 0x20);
		const out = await boundFrameCost(buf, 'image/jpeg');
		assert.ok(out.data.byteLength <= size, `frame grew for size=${size}`);
	}
});
