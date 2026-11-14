// Env settings for bodhi's upstream recovery, and the close classifier it is given.
// Run: npx tsx --test tests/voice-recovery-config.test.ts
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
	DEFAULT_ACTIVE_SILENCE_TICKS,
	DEFAULT_STUCK_CONNECTING_MS,
	MIN_ACTIVE_SILENCE_TICKS,
	MIN_STUCK_CONNECTING_MS,
	activeSilenceTicksFromEnv,
	parseStuckConnectingMs,
	parseActiveSilenceMode,
	parseActiveSilenceTicks,
} from '../src/voice-recovery-config.js';
import { fatalCloseForRecovery } from '../src/voice-error-classifier.js';

describe('active-silence env', () => {
	const w = () => {};
	it('ticks: unset, empty, invalid default; 0 disables; out of range clamps', () => {
		assert.equal(parseActiveSilenceTicks(undefined, w), DEFAULT_ACTIVE_SILENCE_TICKS);
		assert.equal(parseActiveSilenceTicks('   ', w), DEFAULT_ACTIVE_SILENCE_TICKS);
		assert.equal(parseActiveSilenceTicks('0', w), 0);
		assert.equal(parseActiveSilenceTicks('1', w), MIN_ACTIVE_SILENCE_TICKS);
		assert.equal(parseActiveSilenceTicks('100', w), 40);
		assert.equal(parseActiveSilenceTicks('2.5', w), DEFAULT_ACTIVE_SILENCE_TICKS);
		assert.equal(parseActiveSilenceTicks('-1', w), DEFAULT_ACTIVE_SILENCE_TICKS);
	});

	it('mode: default shadow, case-insensitive, invalid warns and is shadow', () => {
		const warns: string[] = [];
		assert.equal(parseActiveSilenceMode(undefined, w), 'shadow');
		assert.equal(parseActiveSilenceMode(' ARMED ', w), 'armed');
		assert.equal(parseActiveSilenceMode('bogus', (m) => warns.push(m)), 'shadow');
		assert.match(warns[0], /VOICE_ACTIVE_SILENCE_MODE/);
	});

	it('bodhi gets ticks only when armed; armed with 0 ticks stays off, with a warning', () => {
		const warns: string[] = [];
		assert.equal(activeSilenceTicksFromEnv({}, w), 0);
		assert.equal(activeSilenceTicksFromEnv({ VOICE_ACTIVE_SILENCE_MODE: 'shadow', VOICE_ACTIVE_SILENCE_TICKS: '5' }, w), 0);
		assert.equal(activeSilenceTicksFromEnv({ VOICE_ACTIVE_SILENCE_MODE: 'Armed' }, w), DEFAULT_ACTIVE_SILENCE_TICKS);
		assert.equal(activeSilenceTicksFromEnv({ VOICE_ACTIVE_SILENCE_MODE: 'armed', VOICE_ACTIVE_SILENCE_TICKS: '5' }, w), 5);
		assert.equal(activeSilenceTicksFromEnv({ VOICE_ACTIVE_SILENCE_MODE: 'armed', VOICE_ACTIVE_SILENCE_TICKS: '0' }, (m) => warns.push(m)), 0);
		assert.match(warns.join('\n'), /disables it; staying off/);
	});
});

describe('stuck-connecting env', () => {
	it('unset defaults; 0 disables and is never clamped; below the floor clamps; invalid warns and defaults', () => {
		const warns: string[] = [];
		const w = (m: string) => warns.push(m);
		assert.equal(parseStuckConnectingMs(undefined, w), DEFAULT_STUCK_CONNECTING_MS);
		assert.equal(parseStuckConnectingMs('0', w), 0);
		assert.equal(parseStuckConnectingMs('5000', w), MIN_STUCK_CONNECTING_MS);
		assert.equal(parseStuckConnectingMs('300000', w), 300_000);
		assert.equal(parseStuckConnectingMs('abc', w), DEFAULT_STUCK_CONNECTING_MS);
		assert.equal(warns.length, 2);
	});
});

describe('fatalCloseForRecovery (upstreamRecovery.classifyClose)', () => {
	it('a non-retryable close is fatal with its category, code and reason', () => {
		assert.deepEqual(fatalCloseForRecovery(1007, 'API key not valid. Please pass a valid API key.'), {
			category: 'auth_invalid',
			code: 1007,
			reason: 'API key not valid. Please pass a valid API key.',
		});
		assert.equal(fatalCloseForRecovery(1011, 'You exceeded your current quota')?.category, 'quota_exceeded');
	});

	it('a retryable close (rate limit, abnormal, normal) is left to the ladder', () => {
		assert.equal(fatalCloseForRecovery(1011, 'Too many requests (429)'), null);
		assert.equal(fatalCloseForRecovery(1006, 'abnormal'), null);
		assert.equal(fatalCloseForRecovery(1000, ''), null);
	});
});
