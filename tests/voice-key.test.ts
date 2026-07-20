import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { voiceApiKey } from '../src/voice-key.js';

const originalMainKey = process.env.GEMINI_API_KEY;
const originalVoiceKey = process.env.GEMINI_VOICE_API_KEY;

afterEach(() => {
	if (originalMainKey === undefined) delete process.env.GEMINI_API_KEY;
	else process.env.GEMINI_API_KEY = originalMainKey;
	if (originalVoiceKey === undefined) delete process.env.GEMINI_VOICE_API_KEY;
	else process.env.GEMINI_VOICE_API_KEY = originalVoiceKey;
});

test('dedicated voice key works without GEMINI_API_KEY', () => {
	delete process.env.GEMINI_API_KEY;
	process.env.GEMINI_VOICE_API_KEY = 'voice-only-key';
	assert.equal(voiceApiKey(), 'voice-only-key');
});

test('dedicated voice key takes precedence over the main key', () => {
	process.env.GEMINI_API_KEY = 'main-key';
	process.env.GEMINI_VOICE_API_KEY = 'voice-key';
	assert.equal(voiceApiKey(), 'voice-key');
});

test('main key remains the fallback', () => {
	process.env.GEMINI_API_KEY = 'main-key';
	delete process.env.GEMINI_VOICE_API_KEY;
	assert.equal(voiceApiKey(), 'main-key');
});
