// Unit tests for the Gemini Live transport-close classifier.
// Patterns derived from real Gemini API close-reason texts observed in
// production logs (see commit message for the failure incident that
// motivated this).

import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
	classifyTransportClose,
	fatalCloseForRecovery,
	isModelUnavailableClose,
	formatVoiceOfflineNotification,
	protocolFailureFor,
	recordTerminalClassification,
	lastTerminalClassification,
	clearTerminalClassification,
	clearRouteRefusalForAttempt,
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

// The close reasons Gemini Live sent for a dead ephemeral token (1011, gemini-3.8-live, 2026-10-17).
const DEAD_TOKEN_REASONS = [
	'Token has expired',
	'Token has been used too many times',
	'new_session_expire_time deadline exceeded',
];

test('token_expired: a dead ephemeral token is terminal, so the redial ladder stops', () => {
	for (const reason of DEAD_TOKEN_REASONS) {
		const r = classifyTransportClose(1011, reason);
		assert.equal(r.category, 'token_expired', reason);
		assert.equal(r.retryable, false, reason);
		assert.match(r.userMessage, /token/i, reason);
		assert.deepEqual(fatalCloseForRecovery(1011, reason), { category: 'token_expired', code: 1011, reason }, reason);
	}
});

test('token_expired: 1011 deadline and outage closes stay retryable', () => {
	// Real transient closes from a voice-agent log: same code, unrelated to the token.
	for (const reason of [
		'Deadline expired before operation could complete.',
		'The service is currently unavailable.',
		'Internal error occurred.',
	]) {
		assert.equal(classifyTransportClose(1011, reason).retryable, true, reason);
		assert.equal(fatalCloseForRecovery(1011, reason), null, reason);
	}
});

test('model_not_found: configured model unavailable', () => {
	const r = classifyTransportClose(
		1011,
		'models/gemini-3.1-flash-live-preview is not found for API version v1beta',
	);
	assert.equal(r.category, 'model_not_found');
	assert.equal(r.retryable, false);
});

test('model_not_found: the fix it names is the workspace voice config, where voice-agent reads its model', () => {
	const r = classifyTransportClose(
		1008,
		'models/gemini-3.8-live is not found for API version v1beta, or is not supported for bidiGenerateContent.',
	);
	assert.equal(r.category, 'model_not_found');
	assert.match(r.userMessage, /config\/voice-agent\.json/);
	assert.doesNotMatch(r.userMessage, /\.env\b|VOICE_NATIVE_AUDIO_MODEL/);
	assert.match(formatVoiceOfflineNotification(r.userMessage, new Date(0)), /config\/voice-agent\.json/);
});

// Google's verbatim region refusal, as Gemini Live closes with it (1007).
const REGION_REFUSAL = 'User location is not supported for the API use.';

test('location_unsupported: Google\'s region refusal names the region and the VPN route, not the model or a key', () => {
	const r = classifyTransportClose(1007, REGION_REFUSAL);
	assert.equal(r.category, 'location_unsupported');
	assert.equal(r.retryable, false);
	assert.match(r.userMessage, /isn't available from your network's region/);
	assert.match(r.userMessage, /user location not supported/);
	assert.match(r.userMessage, /VPN or proxy, route generativelanguage\.googleapis\.com through a supported region/);
	assert.doesNotMatch(r.userMessage, /config\/voice-agent\.json|model|API key|GEMINI_API_KEY/i);
	assert.equal(r.userActionUrl, 'https://aistudio.google.com/docs/available-regions');
	assert.equal(isModelUnavailableClose(1007, REGION_REFUSAL), false);
});

test('location_unsupported: shown as terminal, but recovery keeps dialing, so a fixed route brings voice back', () => {
	assert.equal(classifyTransportClose(1007, REGION_REFUSAL).retryable, false);
	assert.equal(fatalCloseForRecovery(1007, REGION_REFUSAL), null, 'no 5-minute fatal backoff for a region refusal');
	// Every other terminal category still holds dialing for the fatal backoff.
	for (const [code, reason, category] of [
		[1011, 'Your prepayment credits are depleted.', 'credits_depleted'],
		[1011, 'You exceeded your current quota, please check your plan and billing details.', 'quota_exceeded'],
		[1007, 'API key not valid. Please pass a valid API key.', 'auth_invalid'],
		[1011, 'Token has expired', 'token_expired'],
		[1008, 'models/gemini-3.8-live is not found for API version v1beta', 'model_not_found'],
	] as Array<[number, string, string]>) {
		assert.equal(fatalCloseForRecovery(code, reason)?.category, category, reason);
	}
});

test('clearRouteRefusalForAttempt drops only a persisted region refusal', () => {
	clearTerminalClassification();
	assert.equal(clearRouteRefusalForAttempt(), false, 'nothing persisted');

	recordTerminalClassification(classifyTransportClose(1007, REGION_REFUSAL));
	assert.equal(clearRouteRefusalForAttempt(), true);
	assert.equal(lastTerminalClassification(), null);

	const quota = recordTerminalClassification(classifyTransportClose(1011, 'You exceeded your current quota'));
	assert.equal(clearRouteRefusalForAttempt(), false, 'a quota failure stays until ACTIVE');
	assert.deepEqual(lastTerminalClassification(), quota);

	// A region refusal replaced by a quota failure is no longer a route refusal.
	recordTerminalClassification(classifyTransportClose(1007, REGION_REFUSAL));
	recordTerminalClassification(classifyTransportClose(1011, 'You exceeded your current quota'));
	assert.equal(clearRouteRefusalForAttempt(), false);
	clearTerminalClassification();
});

test('model_not_found: a real model refusal still classifies after "not supported for" was narrowed', () => {
	for (const reason of [
		'models/gemini-3.8-live is not found for API version v1beta, or is not supported for bidiGenerateContent.',
		// Close reasons are cut at 123 bytes; this one ends mid-method.
		'models/gemini-live-2.5-flash-preview-native-audio-09-2025 is not found for API version v1main, or is not supported for bidi',
		'models/gemini-2.5-flash-native-audio is not supported for bidiGenerateContent.',
		'models/gemini-2.5-flash is not supported for generateContent.',
		'models/gemini-2.5-flash is not supported for streamGenerateContent.',
	]) {
		const r = classifyTransportClose(1008, reason);
		assert.equal(r.category, 'model_not_found', reason);
		assert.equal(isModelUnavailableClose(1008, reason), true, reason);
	}
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

test('protocol mapper: token_expired → failed/auth with its own reason code', () => {
	for (const reason of DEAD_TOKEN_REASONS) {
		const p = protocolFailureFor(classifyTransportClose(1011, reason));
		assert.deepEqual(p, { upstream: 'failed', reason: 'credential-expired', category: 'auth' }, reason);
	}
});

test('protocol mapper: model_not_found → failed/other', () => {
	const c = classifyTransportClose(1011, 'models/gemini-3.1-flash-live-preview is not found for API version v1beta');
	const p = protocolFailureFor(c);
	assert.deepEqual(p, { upstream: 'failed', reason: 'model-not-found', category: 'other' });
});

test('protocol mapper: location_unsupported → failed/network with its own reason code', () => {
	const p = protocolFailureFor(classifyTransportClose(1007, REGION_REFUSAL));
	assert.deepEqual(p, { upstream: 'failed', reason: 'location-unsupported', category: 'network' });
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
