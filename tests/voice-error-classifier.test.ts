// Unit tests for the Gemini Live transport-close classifier.
// Patterns derived from real Gemini API close-reason texts observed in
// production logs (see commit message for the failure incident that
// motivated this).

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
	classifyTransportClose,
	protocolFailureFor,
	recordTerminalClassification,
	lastTerminalClassification,
	clearTerminalClassification,
} from '../src/voice-error-classifier.ts';

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

// ---------------------------------------------------------------------------
// `agent.state` protocol mapper + persisted terminal classification
// (design 1a′; impl plan WS1 Step 12, amendment R8)
// ---------------------------------------------------------------------------

test('protocol mapper: auth_invalid → failed/auth with stable reason code', () => {
	const c = classifyTransportClose(1011, 'API key not valid. Please pass a valid API key.');
	const p = protocolFailureFor(c);
	assert.deepEqual(p, { upstream: 'failed', reason: 'auth-invalid', category: 'auth' });
});

test('protocol mapper: quota_exceeded → failed/quota — distinct from auth', () => {
	const c = classifyTransportClose(1011, 'You exceeded your current quota, please check your plan and billing details.');
	const p = protocolFailureFor(c);
	assert.deepEqual(p, { upstream: 'failed', reason: 'quota-exceeded', category: 'quota' });
});

test('protocol mapper: credits_depleted → failed/quota with its own reason code', () => {
	const c = classifyTransportClose(1011, 'Your prepayment credits are depleted.');
	const p = protocolFailureFor(c);
	assert.deepEqual(p, { upstream: 'failed', reason: 'credits-depleted', category: 'quota' });
});

test('protocol mapper: model_not_found → failed/other', () => {
	const c = classifyTransportClose(1011, 'models/gemini-3.1-flash-live-preview is not found for API version v1beta');
	const p = protocolFailureFor(c);
	assert.deepEqual(p, { upstream: 'failed', reason: 'model-not-found', category: 'other' });
});

test('protocol mapper: retryable classifications never map to failed', () => {
	for (const [code, reason] of [
		[1011, 'Too Many Requests: rate-limit exceeded'],
		[1000, 'normal close'],
		[1011, 'something we have not seen before'],
		[undefined, undefined],
	] as Array<[number | undefined, string | undefined]>) {
		assert.equal(protocolFailureFor(classifyTransportClose(code, reason)), null,
			`retryable close (${code}, ${reason}) must not map to failed`);
	}
});

test('recordTerminalClassification persists ONLY terminal classifications for buildAgentState', () => {
	clearTerminalClassification();
	assert.equal(lastTerminalClassification(), null);

	// Retryable close: nothing recorded, returns null.
	const r = recordTerminalClassification(classifyTransportClose(1011, 'rate-limit exceeded'));
	assert.equal(r, null);
	assert.equal(lastTerminalClassification(), null);

	// Terminal close: recorded + returned.
	const t = recordTerminalClassification(
		classifyTransportClose(1011, 'API key not valid. Please pass a valid API key.'),
	);
	assert.deepEqual(t, { upstream: 'failed', reason: 'auth-invalid', category: 'auth' });
	assert.deepEqual(lastTerminalClassification(), t);

	// A later retryable close does NOT clear the persisted terminal one —
	// the agent stays 'failed' until recovery (ACTIVE) clears it.
	recordTerminalClassification(classifyTransportClose(1000, 'normal close'));
	assert.deepEqual(lastTerminalClassification(), t);

	// A later terminal close of a DIFFERENT category replaces it (auth → quota).
	recordTerminalClassification(classifyTransportClose(1011, 'You exceeded your current quota'));
	assert.deepEqual(lastTerminalClassification(),
		{ upstream: 'failed', reason: 'quota-exceeded', category: 'quota' });

	// Recovery clears.
	clearTerminalClassification();
	assert.equal(lastTerminalClassification(), null);
});
