// voice-agent.ts hands upstream recovery to bodhi's upstreamRecovery and keeps no redial of its own.
// voice-agent.ts runs main() at import, so this reads the source.
// Run: npx tsx --test tests/voice-upstream-recovery-delegation.test.ts
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const src = readFileSync(join(import.meta.dirname ?? '.', '..', 'src/voice-agent.ts'), 'utf-8');

describe('voice-agent.ts delegates upstream recovery to bodhi', () => {
	it('runs actor mode with the hold policy and bodhi upstreamRecovery', () => {
		assert.match(src, /\t\torchestrationMode: 'actor',\n/);
		assert.match(src, /\t\tupstreamLossPolicy: 'hold',\n\t\tupstreamRecovery: \{\n\t\t\tidleParkMs: IDLE_TEARDOWN_MS,\n/);
	});

	it('classifies fatal closes with the one sutando classifier', () => {
		assert.match(src, /\t\t\tclassifyClose: fatalCloseForRecovery,\n/);
	});

	it('keeps no private redial, park timer or silence coordinator', () => {
		assert.doesNotMatch(src, /\.recoverUpstream\(|handleClientConnected\(\)|suppressClientAutoActions/);
		assert.doesNotMatch(src, /voice-redial-scheduler|voice-connect-watchdog|voice-upstream-recovery|voice-silence-recovery-coordinator/);
		assert.doesNotMatch(src, /idleTeardownTimer|scheduleIdleTeardown/);
	});
});
