// Bounding a frame awaits (`sips`), which opens a window between the admission
// check and the send. If the user stops sharing inside that window, the in-flight
// frame must NOT land: it would arrive after the "screen share stopped" in-band
// note, contradict it, and contaminate the next turn — and it would advance a
// frame counter that stop had already reset.
//
// A post-await `pushMode` re-check is NOT sufficient, because stop followed by a
// new start leaves the flag true again and would re-admit the old frame. Each
// submission is bound to the stream generation captured before the await.
//
// These drive the production `submitFrame` / `startStreaming` / `stopStreaming`
// functions and a real oversized invalid image so the genuine async `sips` path
// runs — no copied policy, no stubbed bound.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
	submitFrame,
	startStreaming,
	stopStreaming,
	getVisionState,
	setVisionSession,
} from '../src/vision-tools.js';

/** 300 KB of non-image bytes — over the passthrough threshold, so boundFrameCost
 *  really shells out to sips and really awaits before failing open. */
function oversizedFrame(): Buffer {
	const b = Buffer.alloc(300 * 1024);
	for (let i = 0; i < b.length; i++) b[i] = (i * 31) & 0xff;
	return b;
}

function fakeSession() {
	const sent: string[] = [];
	return {
		sent,
		session: {
			transport: {
				isConnected: true,
				sendFile: (b64: string) => { sent.push(b64); },
				sendContent: () => {},
			},
		},
	};
}

test('a stop during bounding drops the in-flight frame', async () => {
	const { sent, session } = fakeSession();
	setVisionSession(session);
	startStreaming('browser', undefined, 'push');

	const inFlight = submitFrame(oversizedFrame(), 'image/jpeg');
	const stop = stopStreaming();
	const r = await inFlight;

	assert.equal(stop.status, 'stopped');
	assert.equal(r.ok, false, 'a frame that outlived its stream must not report sent');
	assert.equal(sent.length, 0, 'nothing may reach the transport after stop');
	const st = getVisionState();
	assert.equal(st.streaming, false);
	assert.equal(st.frames, 0, 'stop reset the counter; a stale frame must not advance it');

	setVisionSession(null);
});

test('stop followed by a NEW start still rejects the frame from the old session', async () => {
	// The case a plain post-await `pushMode` check would miss: the flag is true
	// again by the time the old frame resumes.
	const { sent, session } = fakeSession();
	setVisionSession(session);
	startStreaming('browser', undefined, 'push');

	const inFlight = submitFrame(oversizedFrame(), 'image/jpeg');
	stopStreaming();
	startStreaming('browser', undefined, 'push');
	const r = await inFlight;

	assert.equal(getVisionState().streaming, true, 'the new session is live');
	assert.equal(r.ok, false, 'the OLD session\'s frame must still be refused');
	assert.equal(sent.length, 0);
	assert.equal(getVisionState().frames, 0, 'the new session\'s count must not include it');

	stopStreaming();
	setVisionSession(null);
});

test('with no stop, an oversized frame still sends normally', async () => {
	// Guards against closing the race by breaking the feature.
	const { sent, session } = fakeSession();
	setVisionSession(session);
	startStreaming('browser', undefined, 'push');

	const r = await submitFrame(oversizedFrame(), 'image/jpeg');

	assert.equal(r.ok, true);
	assert.equal(sent.length, 1, 'the frame reaches the transport when the stream is still live');
	assert.equal(getVisionState().frames, 1);

	stopStreaming();
	setVisionSession(null);
});
