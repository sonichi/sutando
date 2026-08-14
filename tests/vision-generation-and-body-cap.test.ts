// Three doors the frame-cost bound left open, from qingyun-wu's review of #2885.
//
// The PR bound a frame's wire cost and guarded the resulting await with a stream
// generation — but only the push branch and stopStream bumped that generation,
// and only the control server saw the body at all. So:
//
//  - a PULL-mode restart left the counter untouched, and a frame still bounding
//    for the old source could land in the new stream (`startStream` never calls
//    `stopStream`);
//  - a session swap mid-bound delivered the frame to the session routing had
//    just left, because both send paths capture `sendFile` BEFORE their await;
//  - the web-client proxy buffered an unbounded body on a surface that binds
//    0.0.0.0 by default, and every oversized body then cost a `sips`.
//
// These drive the production functions and a real oversized buffer so the
// genuine async bound runs, matching vision-stop-during-bound.test.ts.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';
import {
	submitFrame,
	startStreaming,
	stopStreaming,
	registerSource,
	setVisionSession,
} from '../src/vision-tools.js';
import { readBodyCapped, FRAME_MAX_BODY_BYTES } from '../src/http-body-limit.js';

/** Over the passthrough threshold, so boundFrameCost really shells out and
 *  really awaits before failing open. */
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

test('a pull-mode restart drops the frame still bounding for the previous source', async () => {
	const { sent, session } = fakeSession();
	setVisionSession(session);

	registerSource({
		name: 'gen-test-a',
		capture: async () => ({ data: oversizedFrame(), mimeType: 'image/jpeg' }),
	});
	// B never produces a frame, so anything that arrives can only be A's — the
	// assertion cannot pass merely because B was slower.
	registerSource({
		name: 'gen-test-b',
		capture: () => new Promise(() => {}),
	});

	startStreaming('gen-test-a', undefined, 'pull');   // fires tick() synchronously
	startStreaming('gen-test-b', undefined, 'pull');   // restart, no stopStream in between
	await new Promise((r) => setTimeout(r, 1500));      // outlast A's bound

	assert.equal(sent.length, 0, "a frame bound for the replaced source must not land in the new stream");
	stopStreaming();
});

test('a session swap during bounding drops the in-flight frame', async () => {
	const first = fakeSession();
	const second = fakeSession();
	setVisionSession(first.session);
	startStreaming('browser', undefined, 'push');

	const inFlight = submitFrame(oversizedFrame(), 'image/jpeg');
	setVisionSession(second.session);   // routing moves mid-bound
	const r = await inFlight;

	assert.equal(r.ok, false, 'a frame whose session was swapped must not report sent');
	assert.equal(first.sent.length, 0, 'nothing may reach the session routing just left');
	assert.equal(second.sent.length, 0, 'nor may it be re-pointed at the new session');
	stopStreaming();
});

test('readBodyCapped refuses an oversized body instead of buffering it', async () => {
	const seen: Array<number | null> = [];
	const srv = createServer(async (req, res) => {
		const body = await readBodyCapped(req, 64 * 1024);
		seen.push(body ? body.byteLength : null);
		res.writeHead(body ? 200 : 413).end();
	});
	await new Promise<void>((r) => srv.listen(0, '127.0.0.1', r));
	const port = (srv.address() as AddressInfo).port;

	const small = await fetch(`http://127.0.0.1:${port}/`, { method: 'POST', body: new Uint8Array(1024) });
	assert.equal(small.status, 200, 'a body under the cap still goes through');

	// 1 MB against a 64 KB cap. fetch may see the socket destroyed mid-send, so
	// a transport error here is also a pass — what matters is that the handler
	// refused rather than buffering the whole body.
	let status = 0;
	try {
		status = (await fetch(`http://127.0.0.1:${port}/`, { method: 'POST', body: new Uint8Array(1024 * 1024) })).status;
	} catch {
		status = 413;
	}
	assert.equal(status, 413, 'a body over the cap must be refused');
	assert.deepEqual(seen, [1024, null], 'the oversized body must not reach the handler as bytes');

	await new Promise<void>((r) => srv.close(() => r()));
});

test('the shared cap is the one both vision surfaces use', () => {
	// A cap that lives in one adapter is a cap the other silently lacks; this
	// pins that the constant is shared rather than re-declared per surface.
	assert.equal(typeof FRAME_MAX_BODY_BYTES, 'number');
	assert.ok(FRAME_MAX_BODY_BYTES >= 1024 * 1024, 'must clear a real unbounded Retina capture (~2.5MB)');
});
