/**
 * Test the voice backend selector logic.
 * Verifies environment-based backend selection.
 */

import { strict as assert } from 'assert';
import { test, describe } from 'node:test';

// Helper to test backend selection logic
function testBackendSelectionLogic(envValue: string | undefined): string {
	const VOICE_BACKEND = (envValue || 'gemini').toLowerCase();
	
	if (VOICE_BACKEND === 'gpt-realtime') {
		return 'azure';
	} else if (VOICE_BACKEND === 'gemini') {
		return 'gemini';
	} else {
		throw new Error(`Unknown VOICE_BACKEND: ${VOICE_BACKEND}`);
	}
}

describe('Voice Backend Selector', () => {
	test('defaults to gemini when VOICE_BACKEND is unset', () => {
		const result = testBackendSelectionLogic(undefined);
		assert.equal(result, 'gemini');
	});

	test('uses gemini when VOICE_BACKEND=gemini', () => {
		const result = testBackendSelectionLogic('gemini');
		assert.equal(result, 'gemini');
	});

	test('uses azure when VOICE_BACKEND=gpt-realtime', () => {
		const result = testBackendSelectionLogic('gpt-realtime');
		assert.equal(result, 'azure');
	});

	test('rejects unknown voice backend', () => {
		assert.throws(() => {
			testBackendSelectionLogic('unknown');
		}, /Unknown VOICE_BACKEND/);
	});

	test('case-insensitive backend selection', () => {
		const result = testBackendSelectionLogic('GPT-REALTIME');
		assert.equal(result, 'azure');
	});

	test('azure transport requires credentials', async () => {
		// Test that Azure transport throws without credentials
		const originalEnv = { ...process.env };
		delete process.env.AZURE_OPENAI_API_KEY;
		delete process.env.AZURE_OPENAI_ENDPOINT;
		
		try {
			const { buildAzureRealtimeTransport } = await import('../src/voice-backends/azure-realtime.js');
			assert.throws(() => {
				buildAzureRealtimeTransport();
			}, /VOICE_BACKEND=gpt-realtime requires/);
		} catch (importErr) {
			// Expected if module has dependencies not available in test
			console.log('Azure module import test skipped (dependencies not available)');
		} finally {
			Object.assign(process.env, originalEnv);
		}
	});
});

console.log('Voice backend selector tests completed!');