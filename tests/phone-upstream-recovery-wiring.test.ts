// The phone server's upstream-recovery wiring, driven through a real bodhi EventBus.
// Run: npx tsx --test --test-force-exit tests/phone-upstream-recovery-wiring.test.ts
import { afterEach, beforeEach, describe, it, mock } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { EventBus, type RecoverUpstreamArgs } from 'bodhi-realtime-agent';
import {
	POST_PARK_REDIAL_DELAY_MS,
	phoneCallIsLive,
	wirePhoneUpstreamRecovery,
	type PhoneRecoverySession,
} from '../skills/phone-conversation/scripts/upstream-recovery-wiring.js';

const quiet = { log: () => {}, error: () => {} };

function parkedCall(callSid = 'CA1') {
	const recovered: RecoverUpstreamArgs[] = [];
	const eventBus = new EventBus();
	const session: PhoneRecoverySession = {
		eventBus,
		sessionManager: { state: 'UPSTREAM_LOST' },
		getRecoveryCapabilities: () => ({ recoverUpstream: true }),
		recoverUpstream: (args) => { recovered.push(args); return { activated: Promise.resolve() }; },
		handleClientConnected: () => assert.fail('the phone has no legacy reconnect'),
	};
	const callSession = { callSid, hangingUp: false };
	const activeCalls = new Map<string, unknown>([[callSid, callSession]]);
	const lose = () => eventBus.publish('session.upstreamLost', { sessionId: 's', reason: 'transport-close', code: 1011 });
	return { session, callSession, activeCalls, recovered, lose };
}

describe('phoneCallIsLive', () => {
	it('is true only for a registered call that is not hanging up', () => {
		assert.equal(phoneCallIsLive({ callSid: 'CA1', hangingUp: false }, new Set(['CA1'])), true);
		assert.equal(phoneCallIsLive({ callSid: 'CA1', hangingUp: true }, new Set(['CA1'])), false);
		assert.equal(phoneCallIsLive({ callSid: 'CA1', hangingUp: false }, new Set(['CA2'])), false);
	});
});

describe('wirePhoneUpstreamRecovery', () => {
	beforeEach(() => mock.timers.enable({ apis: ['setTimeout'] }));
	afterEach(() => mock.timers.reset());

	it('redials a parked live call after the delay, holding synthetic output until fresh speech', () => {
		const c = parkedCall();
		wirePhoneUpstreamRecovery({ session: c.session, callSession: c.callSession, activeCalls: c.activeCalls, ...quiet });
		c.lose();
		mock.timers.tick(POST_PARK_REDIAL_DELAY_MS - 1);
		assert.equal(c.recovered.length, 0, 'redialled before the delay');
		mock.timers.tick(1);
		assert.deepEqual(c.recovered, [{ reason: 'human-retry', skipContextInjection: false, holdSyntheticUntilFreshSpeech: true }]);
		assert.equal(POST_PARK_REDIAL_DELAY_MS, 1500);
	});

	it('does not redial a call that hung up before the delay elapsed', () => {
		const c = parkedCall();
		wirePhoneUpstreamRecovery({ session: c.session, callSession: c.callSession, activeCalls: c.activeCalls, ...quiet });
		c.lose();
		c.callSession.hangingUp = true;
		mock.timers.tick(POST_PARK_REDIAL_DELAY_MS);
		assert.equal(c.recovered.length, 0);
	});

	it('does not redial a call that left activeCalls', () => {
		const c = parkedCall();
		wirePhoneUpstreamRecovery({ session: c.session, callSession: c.callSession, activeCalls: c.activeCalls, ...quiet });
		c.lose();
		c.activeCalls.delete('CA1');
		mock.timers.tick(POST_PARK_REDIAL_DELAY_MS);
		assert.equal(c.recovered.length, 0);
	});

	it('logs the loss with its reason and close code', () => {
		const c = parkedCall();
		const lines: string[] = [];
		wirePhoneUpstreamRecovery({ session: c.session, callSession: c.callSession, activeCalls: c.activeCalls, log: (m) => { lines.push(m); }, error: () => {} });
		c.lose();
		assert.match(lines.join('\n'), /\[Phone\] upstream lost: reason=transport-close code=1011 detail=-/);
	});

	it('runs onActivated once the redial activates', async () => {
		const c = parkedCall();
		let activated = 0;
		wirePhoneUpstreamRecovery({
			session: c.session, callSession: c.callSession, activeCalls: c.activeCalls,
			onActivated: () => { activated++; }, ...quiet,
		});
		c.lose();
		mock.timers.tick(POST_PARK_REDIAL_DELAY_MS);
		mock.timers.reset();
		await new Promise((r) => setImmediate(r));
		assert.equal(activated, 1);
	});
});

// Structural delegation pin (REVIEW.md lesson 14): conversation-server.ts cannot be imported by a test,
// so this checks it hands its own session and call registry to the tested wiring and keeps no copy.
describe('conversation-server.ts delegates upstream recovery', () => {
	const src = readFileSync(join(import.meta.dirname ?? '.', '..', 'skills/phone-conversation/scripts/conversation-server.ts'), 'utf-8');

	it('wires every call session through wirePhoneUpstreamRecovery', () => {
		assert.match(src, /^\twirePhoneUpstreamRecovery\(\{\n\t\tsession: session as unknown as PhoneRecoverySession,\n\t\tcallSession,\n\t\tactiveCalls,\n/m);
	});

	it('keeps no private upstreamLost handler or redial', () => {
		assert.doesNotMatch(src, /subscribe\(\s*['"]session\.upstreamLost/);
		assert.doesNotMatch(src, /createPostParkRedialer|redialUpstream\(|\.recoverUpstream\(/);
	});
});
