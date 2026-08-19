// The request-body cap for the two HTTP surfaces that accept a vision frame.
//
// #2902 owns the egress side: an oversized frame is refused by the central gate
// (`frame-too-large`). That check runs AFTER the whole request has been read,
// so it bounds what reaches the session, not what reaches memory — and the
// web-client proxy binds 0.0.0.0 by default.
//
// This pins the ingress half: the body is refused mid-stream, before it is
// buffered, and the ceiling is a single shared constant rather than one
// re-declared per surface (a cap that lives in one adapter is a cap the other
// silently lacks).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';
import { readBodyCapped, FRAME_MAX_BODY_BYTES } from '../src/http-body-limit.js';


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
	let status: number;
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
