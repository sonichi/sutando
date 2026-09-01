/**
 * switch_voice_config must PRESERVE the user's config file and overlay only
 * the two keys it owns.
 *
 * The previous form wrote `{...VOICE_CONFIG_DEFAULTS, ...preset}` without
 * ever reading the file, so every switch replaced it — silently deleting
 * session tuning (`compressionConfig`, `mediaResolution`), their explicit
 * `null`/`false` off-switches, and any future key, with a restart right
 * behind it so the loss left no trace. Two consequences this file pins
 * against: a canary soak cannot be reverted mid-measurement, and a
 * fleet-wide defaults revert still reaches devices whose user has used the
 * switch.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { nextSwitchConfig, readConfigRaw } from '../src/voice-config-switch.js';
import { VOICE_CONFIG_DEFAULTS, type VoiceConfig } from '../src/voice-config.js';

const SEARCH: Pick<VoiceConfig, 'model' | 'googleSearch'> = {
	model: 'gemini-2.5-flash-native-audio-preview-12-2025',
	googleSearch: true,
};

test('the preset overlays model + googleSearch and nothing else', () => {
	const existing = {
		model: 'gemini-3.1-flash-live-preview',
		googleSearch: false,
		shadowStt: true,
		channels: { c1: { owner_mode: true } },
	};
	const next = nextSwitchConfig(existing, SEARCH);
	assert.equal(next.model, SEARCH.model);
	assert.equal(next.googleSearch, true);
	assert.equal(next.shadowStt, true, 'unrelated user settings survive');
	assert.deepEqual(next.channels, { c1: { owner_mode: true } });
});

test('session tuning survives a switch — a canary soak is not silently reverted', () => {
	const existing = {
		compressionConfig: {},
		mediaResolution: 'MEDIA_RESOLUTION_LOW',
	};
	const next = nextSwitchConfig(existing, SEARCH) as VoiceConfig;
	assert.deepEqual(next.compressionConfig, {}, 'canary 3b key retained');
	assert.equal(next.mediaResolution, 'MEDIA_RESOLUTION_LOW', 'canary 3c key retained');
});

test('an explicit off-switch survives — a defaults revert cannot be undone by a switch', () => {
	for (const off of [null, false] as const) {
		const next = nextSwitchConfig(
			{ compressionConfig: off, mediaResolution: off },
			SEARCH,
		) as VoiceConfig;
		assert.equal(next.compressionConfig, off, `${JSON.stringify(off)} preserved`);
		assert.equal(next.mediaResolution, off);
	}
});

test('unknown future keys are preserved verbatim', () => {
	const next = nextSwitchConfig({ someFutureKnob: 42 }, SEARCH) as VoiceConfig &
		Record<string, unknown>;
	assert.equal(next.someFutureKnob, 42);
});

test('a missing, corrupt, or non-object file falls back to defaults, never refuses', () => {
	const dir = mkdtempSync(join(tmpdir(), 'switchcfg-'));
	try {
		const missing = join(dir, 'nope.json');
		assert.equal(readConfigRaw(missing), null);

		const corrupt = join(dir, 'corrupt.json');
		writeFileSync(corrupt, '{ not json');
		assert.equal(readConfigRaw(corrupt), null);

		const arrayFile = join(dir, 'array.json');
		writeFileSync(arrayFile, '[1,2,3]');
		const next = nextSwitchConfig(readConfigRaw(arrayFile), SEARCH);
		assert.equal(next.shadowStt, VOICE_CONFIG_DEFAULTS.shadowStt, 'defaults fill in');
		assert.equal(next.model, SEARCH.model);
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
});

test('a real file round-trips through readConfigRaw', () => {
	const dir = mkdtempSync(join(tmpdir(), 'switchcfg-'));
	try {
		const p = join(dir, 'voice-agent.json');
		writeFileSync(p, JSON.stringify({ shadowStt: true, compressionConfig: {} }, null, 2));
		const next = nextSwitchConfig(readConfigRaw(p), SEARCH) as VoiceConfig;
		assert.equal(next.shadowStt, true);
		assert.deepEqual(next.compressionConfig, {});
		assert.equal(next.googleSearch, true, 'preset still wins on its own keys');
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
});
