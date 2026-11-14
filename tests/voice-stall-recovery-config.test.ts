// bodhi's stage-one stall recovery (replay the retained user utterance in place) is behind a flag sutando never set.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

test('the voice session turns on in-place utterance replay and exposes the watchdog silence', () => {
	const src = readFileSync(join(process.cwd(), 'src', 'voice-agent.ts'), 'utf-8');
	assert.match(src, /watchdogReplayRecovery: VOICE_WATCHDOG_REPLAY,/);
	assert.match(src, /responseWatchdogMs: VOICE_RESPONSE_WATCHDOG_MS,/);
	assert.match(src, /process\.env\.VOICE_WATCHDOG_REPLAY !== '0'/, 'on unless set to 0');
});
