/**
 * The "latest-search" preset pairs the newest live model with Web grounding and is
 * reachable by name through the switch tool's schema.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { PRESETS, switchVoiceConfigTool, nextSwitchConfig } from '../src/voice-config-switch.js';

test('latest-search is the newest live model with googleSearch on', () => {
	assert.deepEqual(PRESETS['latest-search'], { model: 'gemini-3.1-flash-live-preview', googleSearch: true });
	assert.equal(PRESETS['no-search'].model, PRESETS['latest-search'].model, 'same model as no-search; the grounding knob differs');
	assert.equal(PRESETS.search.googleSearch, true);
});

test('the tool schema accepts latest-search and still refuses an unknown name', () => {
	const schema = (switchVoiceConfigTool as unknown as { parameters: { safeParse: (v: unknown) => { success: boolean } } }).parameters;
	assert.equal(schema.safeParse({ preset: 'latest-search' }).success, true);
	assert.equal(schema.safeParse({ preset: 'latest' }).success, false);
});

test('switching to latest-search overlays only model + googleSearch', () => {
	const existing = { model: 'gemini-2.5-flash-native-audio-preview-12-2025', googleSearch: false, owner_mode: true, shadowStt: true };
	const next = nextSwitchConfig(existing as never, PRESETS['latest-search']) as Record<string, unknown>;
	assert.equal(next.model, 'gemini-3.1-flash-live-preview');
	assert.equal(next.googleSearch, true);
	assert.equal(next.owner_mode, true, 'unrelated knobs survive');
	assert.equal(next.shadowStt, true);
});
