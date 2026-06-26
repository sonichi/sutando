import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = readFileSync(
	join(import.meta.dirname ?? '.', '..', 'src/voice-agent.ts'),
	'utf8',
);

describe('voice-agent Gemini API key validation', () => {
	it('does not pin Google AI Studio keys to the legacy AIza prefix', () => {
		assert.doesNotMatch(
			SRC,
			/startsWith\(['"]AIza['"]\)|expected ["']AIza/,
			'AI Studio now issues multiple API key formats; startup must not reject valid non-AIza keys.',
		);
	});

	it('still rejects obvious placeholder values', () => {
		assert.match(
			SRC,
			/value !== ['"]your-gemini-key['"]/,
			'startup should still fail fast for the .env.example placeholder key.',
		);
	});
});
