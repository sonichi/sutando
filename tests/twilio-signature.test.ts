import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';

// Replicate the validation algorithm to test it
function computeTwilioSignature(authToken: string, url: string, params: Record<string, string>): string {
	let s = url;
	for (const [key, value] of Object.entries(params).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)) {
		s += key + value;
	}
	return createHmac('sha1', authToken).update(s).digest('base64');
}

describe('Twilio signature validation algorithm', () => {
	it('computes correct signature for standard params', () => {
		const token = 'test-auth-token-12345';
		const url = 'https://example.com/twilio/voice';
		const params = { CallSid: 'CA123', From: '+15551234567' };
		const sig = computeTwilioSignature(token, url, params);
		assert.ok(sig.length > 0);
		// Verify deterministic
		assert.equal(sig, computeTwilioSignature(token, url, params));
	});

	it('parameter order matters — sorted alphabetically', () => {
		const token = 'secret';
		const url = 'https://example.com/twilio/voice';
		const sig1 = computeTwilioSignature(token, url, { A: '1', B: '2' });
		const sig2 = computeTwilioSignature(token, url, { B: '2', A: '1' });
		assert.equal(sig1, sig2); // same after sorting
	});

	it('different token produces different signature', () => {
		const url = 'https://example.com/twilio/voice';
		const params = { From: '+1234' };
		const sig1 = computeTwilioSignature('token-a', url, params);
		const sig2 = computeTwilioSignature('token-b', url, params);
		assert.notEqual(sig1, sig2);
	});

	it('different URL produces different signature', () => {
		const token = 'secret';
		const params = { From: '+1234' };
		const sig1 = computeTwilioSignature(token, 'https://a.com/twilio/voice', params);
		const sig2 = computeTwilioSignature(token, 'https://b.com/twilio/voice', params);
		assert.notEqual(sig1, sig2);
	});
});
