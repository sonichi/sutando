import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

/**
 * Tests for voiceApiKey() (src/voice-key.ts) — the shared Gemini API key
 * resolver used by all three voice surfaces (voice-agent, phone-conversation,
 * discord-voice). The chain is GEMINI_VOICE_API_KEY → GEMINI_API_KEY → ''.
 *
 * Covers:
 *   1. Neither key set → empty string.
 *   2. Only GEMINI_API_KEY set → returned.
 *   3. Only GEMINI_VOICE_API_KEY set → returned.
 *   4. Both set → GEMINI_VOICE_API_KEY wins (voice-tier isolation preserved).
 */

import { voiceApiKey } from '../src/voice-key.js';

function clearEnv() {
	delete process.env.GEMINI_VOICE_API_KEY;
	delete process.env.GEMINI_API_KEY;
}

describe('voiceApiKey() — env resolution chain', () => {
	it('returns empty string when neither key is set', () => {
		clearEnv();
		assert.equal(voiceApiKey(), '');
	});

	it('returns GEMINI_API_KEY when only main key is set', () => {
		clearEnv();
		process.env.GEMINI_API_KEY = 'main-key-123';
		try {
			assert.equal(voiceApiKey(), 'main-key-123');
		} finally {
			clearEnv();
		}
	});

	it('returns GEMINI_VOICE_API_KEY when only voice key is set', () => {
		clearEnv();
		process.env.GEMINI_VOICE_API_KEY = 'voice-key-abc';
		try {
			assert.equal(voiceApiKey(), 'voice-key-abc');
		} finally {
			clearEnv();
		}
	});

	it('prefers GEMINI_VOICE_API_KEY over GEMINI_API_KEY when both are set', () => {
		clearEnv();
		process.env.GEMINI_VOICE_API_KEY = 'voice-preferred';
		process.env.GEMINI_API_KEY = 'main-fallback';
		try {
			assert.equal(voiceApiKey(), 'voice-preferred');
		} finally {
			clearEnv();
		}
	});
});
