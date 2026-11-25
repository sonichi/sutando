import assert from 'node:assert/strict';
import { PassThrough } from 'node:stream';
import type { IncomingMessage } from 'node:http';
import { test } from 'node:test';
import { createHmac } from 'node:crypto';
import {
	mediaStreamStartCallSid,
	readTwilioBody,
	twilioWebhookDecision,
} from '../skills/phone-conversation/scripts/twilio_request_auth.js';

const TOKEN = 'test-auth-token';

function sign(url: string, params: URLSearchParams): string {
	let signed = url;
	for (const key of [...new Set(params.keys())].sort()) {
		for (const value of params.getAll(key).sort()) signed += key + value;
	}
	return createHmac('sha1', TOKEN).update(signed).digest('base64');
}

test('HTTP Twilio gate accepts signed requests and rejects unsigned or changed bodies', () => {
	const url = 'https://phone.example/twilio/connect';
	const body = 'CallSid=CA123&From=%2B14155550100';
	const signature = sign(url, new URLSearchParams(body));
	assert.deepEqual(twilioWebhookDecision(TOKEN, signature, url, body), { ok: true });
	assert.deepEqual(twilioWebhookDecision(TOKEN, undefined, url, body), {
		ok: false, status: 403, error: 'invalid Twilio signature',
	});
	assert.deepEqual(twilioWebhookDecision(TOKEN, signature, url, `${body}&To=other`), {
		ok: false, status: 403, error: 'invalid Twilio signature',
	});
});

test('HTTP Twilio gate returns 413 before checking a body signature', () => {
	assert.deepEqual(twilioWebhookDecision(TOKEN, undefined, 'https://phone.example/twilio/connect', '', true), {
		ok: false, status: 413, error: 'Twilio request body exceeds 64 KiB',
	});
});

test('a media stream start must carry the same CallSid natively and as a parameter', () => {
	const start = { callSid: 'CA123', customParameters: { callSid: 'CA123' } };
	assert.equal(mediaStreamStartCallSid(start), 'CA123');
	assert.equal(
		mediaStreamStartCallSid({ callSid: 'CA123', customParameters: { callSid: 'CA999' } }),
		null,
	);
	assert.equal(mediaStreamStartCallSid({ callSid: 'CA123' }), null);
});

test('oversized Twilio bodies are paused before their contents can be checked', async () => {
	const req = new PassThrough() as unknown as IncomingMessage;
	(req as unknown as { headers: Record<string, string> }).headers = {};
	const bodyRead = readTwilioBody(req);
	(req as unknown as PassThrough).write(Buffer.alloc(64 * 1024 + 1));
	const result = await bodyRead;
	assert.deepEqual(result, { body: '', tooLarge: true });
	assert.equal((req as unknown as PassThrough).isPaused(), true);
});
