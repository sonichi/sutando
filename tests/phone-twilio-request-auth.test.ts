import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import type { IncomingHttpHeaders } from 'node:http';
import {
	isValidMediaStreamUpgrade,
	isValidTwilioSignature,
	mediaStreamStartCallSid,
	mediaStreamUrl,
	twilioSignature,
} from '../skills/phone-conversation/scripts/twilio_request_auth.js';

const TOKEN = 'test-auth-token';
const BASE = 'https://phone.example/path';

function sign(url: string, params: URLSearchParams): string {
	let signed = url;
	for (const key of [...new Set(params.keys())].sort()) {
		for (const value of params.getAll(key).sort()) signed += key + value;
	}
	return createHmac('sha1', TOKEN).update(signed).digest('base64');
}

describe('Twilio request authentication', () => {
	it('accepts a valid form signature', () => {
		const url = 'https://phone.example/twilio/connect?to=%2B14155550100';
		const params = new URLSearchParams('CallSid=CA123&From=%2B14155550100');
		assert.equal(isValidTwilioSignature(TOKEN, sign(url, params), url, params), true);
	});

	it('rejects a tampered form parameter, wrong URL, missing header, and empty token', () => {
		const url = 'https://phone.example/twilio/connect';
		const params = new URLSearchParams('From=%2B14155550100');
		const signature = sign(url, params);
		assert.equal(isValidTwilioSignature(TOKEN, signature, url, new URLSearchParams('From=%2B14155550101')), false);
		assert.equal(isValidTwilioSignature(TOKEN, signature, 'https://other.example/twilio/connect', params), false);
		assert.equal(isValidTwilioSignature(TOKEN, undefined, url, params), false);
		assert.equal(isValidTwilioSignature('', signature, url, params), false);
		assert.equal(isValidTwilioSignature(TOKEN, signature, '', params), false);
	});

	it('sorts duplicate parameter values within sorted unique keys', () => {
		const url = 'https://phone.example/twilio/connect';
		const params = new URLSearchParams();
		params.append('B', 'z');
		params.append('A', 'b');
		params.append('A', 'a');
		const expected = createHmac('sha1', TOKEN)
			.update(`${url}AaAbBz`)
			.digest('base64');
		assert.equal(twilioSignature(TOKEN, url, params), expected);
		assert.equal(isValidTwilioSignature(TOKEN, expected, url, params), true);
	});

	it('builds the same bare media-stream URL as the server TwiML', () => {
		// <Stream url> does not carry query parameters; the CallSid travels in a <Parameter>.
		assert.equal(mediaStreamUrl(BASE), 'wss://phone.example/media-stream');
	});

	it('accepts media-stream signatures over WSS and HTTPS for the stream URL', () => {
		const wsUrl = mediaStreamUrl(BASE);
		const httpsUrl = wsUrl.replace(/^wss:/, 'https:');
		const headers = (signature: string): IncomingHttpHeaders => ({ 'x-twilio-signature': signature });
		assert.equal(isValidMediaStreamUpgrade(TOKEN, headers(sign(wsUrl, new URLSearchParams())), BASE), true);
		assert.equal(isValidMediaStreamUpgrade(TOKEN, headers(sign(httpsUrl, new URLSearchParams())), BASE), true);
		assert.equal(isValidMediaStreamUpgrade(TOKEN, headers(sign('wss://other.example/media-stream', new URLSearchParams())), BASE), false);
		assert.equal(isValidMediaStreamUpgrade(TOKEN, {}, BASE), false);
		assert.equal(isValidMediaStreamUpgrade(TOKEN, headers(sign(wsUrl, new URLSearchParams())), ''), false);
		assert.equal(isValidMediaStreamUpgrade(TOKEN, headers(sign(wsUrl, new URLSearchParams())), 'not a URL'), false);
	});

	it('rejects a stream signature computed over a URL carrying a callSid query', () => {
		const headers: IncomingHttpHeaders = {
			'x-twilio-signature': sign('wss://phone.example/media-stream?callSid=CA123', new URLSearchParams()),
		};
		assert.equal(isValidMediaStreamUpgrade(TOKEN, headers, BASE), false);
	});

	it('binds a stream start only when the native and parameter callSids agree', () => {
		assert.equal(
			mediaStreamStartCallSid({ callSid: 'CA1', customParameters: { callSid: 'CA1' } }),
			'CA1',
		);
		assert.equal(
			mediaStreamStartCallSid({ callSid: 'CA1', customParameters: { callSid: 'CA9' } }),
			null,
		);
		assert.equal(mediaStreamStartCallSid({ callSid: 'CA1' }), null);
		assert.equal(mediaStreamStartCallSid({ customParameters: { callSid: 'CA1' } }), null);
		assert.equal(
			mediaStreamStartCallSid({ callSid: 42, customParameters: { callSid: '42' } }),
			null,
		);
		assert.equal(mediaStreamStartCallSid(null), null);
	});
});
