// The voice session turns on bodhi's echo guard unless VOICE_ECHO_GUARD=0, so the model's own voice
// from the speakers does not fire false barge-ins.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const src = readFileSync(join(import.meta.dirname ?? '.', '..', 'src', 'voice-agent.ts'), 'utf-8');

test('the voice session passes echoGuard, on unless VOICE_ECHO_GUARD=0', () => {
	assert.match(src, /const VOICE_ECHO_GUARD = process\.env\.VOICE_ECHO_GUARD !== '0';/);
	assert.match(src, /echoGuard: \{ enabled: VOICE_ECHO_GUARD \},/);
});

test('no external TTS provider: the echo guard compares against the native audio it is fed', () => {
	assert.doesNotMatch(src, /ttsProvider:/);
});
