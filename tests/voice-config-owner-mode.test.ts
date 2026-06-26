// Unit tests for resolveOwnerMode (src/voice-config.ts) — the fail-closed
// owner-mode resolver for voice-surface configs (issue #1016, PR #1017 review fix).
//
// The surface config is raw JSON spread into VoiceConfig, so a
// hand-edited file can carry a non-boolean owner_mode (string "false", null,
// a number, a typo). A loose `?? false` / truthy check would treat the STRING
// "false" as truthy and grant owner tier to every speaker — a trust-boundary
// bug. resolveOwnerMode must grant ONLY on the boolean literal `true`, and
// must preserve channel-over-skill precedence (a channel-explicit `false`
// overrides a skill-default `true`).

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { resolveOwnerMode, type VoiceConfig } from '../src/voice-config.ts';

// A VoiceConfig is `{ model, googleSearch, owner_mode, channels }`, but the
// owner_mode/channels fields are the only ones resolveOwnerMode reads. Cast
// loosely so we can exercise the malformed shapes a real edited file produces.
function cfg(partial: Partial<VoiceConfig> & Record<string, unknown>): VoiceConfig {
	return {
		model: 'gemini-2.5-flash-native-audio-preview-12-2025',
		googleSearch: true,
		owner_mode: false,
		channels: {},
		...partial,
	} as VoiceConfig;
}

const CH = '1234567890123456789';

test('boolean true at skill level → owner', () => {
	assert.equal(resolveOwnerMode(cfg({ owner_mode: true })), true);
});

test('boolean false at skill level → non-owner', () => {
	assert.equal(resolveOwnerMode(cfg({ owner_mode: false })), false);
});

test('string "true" at skill level fails closed → non-owner', () => {
	assert.equal(resolveOwnerMode(cfg({ owner_mode: 'true' as unknown as boolean })), false);
});

test('string "false" at skill level fails closed → non-owner (would be truthy under ??)', () => {
	// The headline bug: a non-empty string is truthy. `?? false` / `value ||`
	// would grant owner here. Strict `=== true` denies.
	assert.equal(resolveOwnerMode(cfg({ owner_mode: 'false' as unknown as boolean })), false);
});

test('null at skill level fails closed → non-owner', () => {
	assert.equal(resolveOwnerMode(cfg({ owner_mode: null as unknown as boolean })), false);
});

test('number 1 at skill level fails closed → non-owner', () => {
	assert.equal(resolveOwnerMode(cfg({ owner_mode: 1 as unknown as boolean })), false);
});

test('missing channelId arg → uses skill-level only', () => {
	assert.equal(resolveOwnerMode(cfg({ owner_mode: true })), true);
	assert.equal(resolveOwnerMode(cfg({ owner_mode: false })), false);
});

test('channelId given but no channel entry → falls back to skill-level', () => {
	assert.equal(resolveOwnerMode(cfg({ owner_mode: true }), CH), true);
	assert.equal(resolveOwnerMode(cfg({ owner_mode: false }), CH), false);
});

test('channel entry without owner_mode key → falls back to skill-level', () => {
	// Channel entry exists but carries no owner_mode key (e.g. some future
	// per-channel knob). Must defer to the skill-wide default.
	assert.equal(
		resolveOwnerMode(cfg({ owner_mode: true, channels: { [CH]: {} } }), CH),
		true,
	);
	assert.equal(
		resolveOwnerMode(cfg({ owner_mode: false, channels: { [CH]: {} } }), CH),
		false,
	);
});

test('channel owner_mode:true → owner (overrides skill false)', () => {
	assert.equal(
		resolveOwnerMode(cfg({ owner_mode: false, channels: { [CH]: { owner_mode: true } } }), CH),
		true,
	);
});

test('PRECEDENCE: channel-explicit false overrides skill-default true → non-owner', () => {
	// The critical opt-out case: a channel that explicitly opts OUT must not
	// be re-granted by the skill-wide default. An OR-collapse would break this.
	assert.equal(
		resolveOwnerMode(cfg({ owner_mode: true, channels: { [CH]: { owner_mode: false } } }), CH),
		false,
	);
});

test('malformed channel owner_mode (string "true") fails closed → non-owner', () => {
	// Channel entry HAS an owner_mode key but it's a string. Present-but-not-
	// boolean-true must deny — and must NOT fall through to the skill default.
	assert.equal(
		resolveOwnerMode(
			cfg({ owner_mode: true, channels: { [CH]: { owner_mode: 'true' as unknown as boolean } } }),
			CH,
		),
		false,
	);
});

test('malformed channel owner_mode (null) fails closed → non-owner', () => {
	assert.equal(
		resolveOwnerMode(
			cfg({ owner_mode: true, channels: { [CH]: { owner_mode: null as unknown as boolean } } }),
			CH,
		),
		false,
	);
});

test('malformed channel owner_mode (number) fails closed → non-owner', () => {
	assert.equal(
		resolveOwnerMode(
			cfg({ owner_mode: true, channels: { [CH]: { owner_mode: 1 as unknown as boolean } } }),
			CH,
		),
		false,
	);
});

test('default config (owner_mode:false, no channels) → non-owner', () => {
	assert.equal(resolveOwnerMode(cfg({}), CH), false);
});
