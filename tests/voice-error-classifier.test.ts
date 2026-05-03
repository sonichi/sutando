// Unit tests for the Gemini Live transport-close classifier.
// Patterns derived from real Gemini API close-reason texts observed in
// production logs (see commit message for the failure incident that
// motivated this).

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { classifyTransportClose } from '../src/voice-error-classifier.ts';

test('credits_depleted: paid-tier prepayment exhausted', () => {
	const r = classifyTransportClose(
		1011,
		'Your prepayment credits are depleted. Please go to AI Studio at https://ai.studio/projects to manage your project and billi',
	);
	assert.equal(r.category, 'credits_depleted');
	assert.equal(r.retryable, false);
	assert.match(r.userMessage, /credits/i);
	assert.equal(r.userActionUrl, 'https://ai.studio/projects');
});

test('quota_exceeded: free-tier RPM/RPD cap with no billing', () => {
	const r = classifyTransportClose(
		1011,
		'You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: h',
	);
	assert.equal(r.category, 'quota_exceeded');
	assert.equal(r.retryable, false);
	assert.match(r.userActionUrl ?? '', /billing/);
});

test('auth_invalid: revoked or malformed key', () => {
	const r1 = classifyTransportClose(1011, 'API key not valid. Please pass a valid API key.');
	assert.equal(r1.category, 'auth_invalid');
	assert.equal(r1.retryable, false);

	const r2 = classifyTransportClose(undefined, 'PERMISSION_DENIED: caller does not have access');
	assert.equal(r2.category, 'auth_invalid');
});

test('model_not_found: configured model unavailable', () => {
	const r = classifyTransportClose(
		1011,
		'models/gemini-3.1-flash-live-preview is not found for API version v1beta',
	);
	assert.equal(r.category, 'model_not_found');
	assert.equal(r.retryable, false);
});

test('rate_limit: transient 429 stays retryable', () => {
	const r = classifyTransportClose(1011, 'Too Many Requests: rate-limit exceeded');
	assert.equal(r.category, 'rate_limit');
	assert.equal(r.retryable, true);
});

test('transient: normal close (1000) keeps retrying', () => {
	const r = classifyTransportClose(1000, 'normal close');
	assert.equal(r.category, 'transient');
	assert.equal(r.retryable, true);
});

test('unknown: unrecognized 1011 reason defaults to retryable', () => {
	// Conservative default — unknown close reasons must not stop the
	// existing reconnect loop. The caller will keep retrying; only
	// matched patterns flip retryable to false.
	const r = classifyTransportClose(1011, 'something we have not seen before');
	assert.equal(r.category, 'unknown');
	assert.equal(r.retryable, true);
});

test('missing reason and code: still produces a result', () => {
	const r = classifyTransportClose(undefined, undefined);
	assert.equal(r.category, 'unknown');
	assert.equal(r.retryable, true);
	assert.equal(r.rawReason, '');
});

test('rawCode and rawReason are preserved', () => {
	const r = classifyTransportClose(1011, 'Your prepayment credits are depleted.');
	assert.equal(r.rawCode, 1011);
	assert.match(r.rawReason, /prepayment/);
});
