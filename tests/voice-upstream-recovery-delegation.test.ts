// voice-agent.ts hands upstream recovery to bodhi's upstreamRecovery; its only redial is the
// verifier wake, which repeats bodhi's real-client attach redial for a ?verify=1 connection.
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

	it('classifies fatal closes with the one sutando classifier, and feeds agent.state the backoff', () => {
		assert.match(src, /\t\t\tclassifyClose: fatalCloseForRecovery,\n/);
		assert.match(src, /\t\t\tonFatal: \(\{ until \}\) => \{\n\t\t\t\tvoiceFatalBackoffUntil = until;\n\t\t\t\temitAgentState\(\);/);
		assert.match(src, /backoffUntil: \(\) => voiceFatalBackoffUntil,/);
	});

	it('reads the stuck-dial override', () => {
		assert.match(src, /\t\t\tstuckConnectingMs: parseStuckConnectingMs\(process\.env\.VOICE_STUCK_CONNECTING_MS\),\n/);
	});

	it('answers ?verify=1 with the immediate frame, the attach redial and the idle restore', () => {
		assert.match(src, /\t\t\.\.\.createVerifierHooks\(\{\n\t\t\temitImmediate: \(\) => emitAgentState\(\{ immediate: true \}\),\n/);
		assert.match(src, /= session\.recoverUpstream\(\{ reason: 'human-retry', skipContextInjection: false, holdSyntheticUntilFreshSpeech: false \}\);/);
		assert.match(src, /\t\t\tarmIdleRestore: \(\) => probeIdleRestore\.arm\(\),\n/);
		assert.match(src, /\t\t\tprobeIdleRestore\.fence\(\);\n/);
	});

	it('keeps no private redial, park timer or silence coordinator', () => {
		// The one redial is the verifier wake: the call bodhi makes when a real client attaches.
		assert.equal(src.match(/\.recoverUpstream\(/g)?.length, 1);
		assert.doesNotMatch(src, /handleClientConnected\(\)|suppressClientAutoActions/);
		assert.doesNotMatch(src, /voice-redial-scheduler|voice-connect-watchdog|voice-upstream-recovery|voice-silence-recovery-coordinator/);
		assert.doesNotMatch(src, /idleTeardownTimer|scheduleIdleTeardown/);
	});
});
