// The phone server's upstream-recovery wiring: bodhi's upstreamRecovery options for a call, and the park watcher.
// Run: npx tsx --test --test-force-exit tests/phone-upstream-recovery-wiring.test.ts
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { EventBus } from 'bodhi-realtime-agent';
import { fatalCloseForRecovery } from '../src/voice-error-classifier.js';
import {
	POST_PARK_REDIAL_DELAY_MS,
	phoneCallIsLive,
	phoneUpstreamRecovery,
	watchPhoneUpstream,
} from '../skills/phone-conversation/scripts/upstream-recovery-wiring.js';

describe('phoneCallIsLive', () => {
	it('is true only for a registered call that is not hanging up', () => {
		const call = { callSid: 'CA1', hangingUp: false };
		assert.equal(phoneCallIsLive(call, new Set(['CA1'])), true);
		assert.equal(phoneCallIsLive({ ...call, hangingUp: true }, new Set(['CA1'])), false);
		assert.equal(phoneCallIsLive(call, new Set()), false);
	});
});

describe('phoneUpstreamRecovery', () => {
	it('redials only while the call is live, holds synthetic output, and never idle-parks', () => {
		const call = { callSid: 'CA1', hangingUp: false };
		const activeCalls = new Map<string, unknown>([['CA1', call]]);
		const opts = phoneUpstreamRecovery(call, activeCalls);
		assert.equal(opts.isLive?.(), true);
		call.hangingUp = true;
		assert.equal(opts.isLive?.(), false);
		call.hangingUp = false;
		activeCalls.delete('CA1');
		assert.equal(opts.isLive?.(), false);
		assert.equal(opts.holdSyntheticUntilFreshSpeech, true);
		assert.equal(opts.parkRedialDelayMs, POST_PARK_REDIAL_DELAY_MS);
		assert.equal(opts.idleParkMs, 0);
		assert.equal(opts.classifyClose, fatalCloseForRecovery, 'the one Gemini classifier, not bodhi\'s copy');
	});
});

describe('watchPhoneUpstream', () => {
	function watched() {
		const eventBus = new EventBus();
		const lines: string[] = [];
		let recovered = 0;
		watchPhoneUpstream({ eventBus, onRecovered: () => { recovered++; }, log: (m) => lines.push(m) });
		const state = (fromState: string, toState: string) =>
			eventBus.publish('session.stateChange', { sessionId: 's', fromState, toState } as never);
		const lose = () => eventBus.publish('session.upstreamLost', { sessionId: 's', reason: 'reconnect-exhausted', code: 1011 });
		return { lines, recovered: () => recovered, state, lose };
	}

	it('logs the loss with its reason and close code', () => {
		const w = watched();
		w.lose();
		assert.match(w.lines[0] ?? '', /upstream lost: reason=reconnect-exhausted code=1011/);
	});

	it('runs onRecovered once when a parked call is back to ACTIVE, and not for an ordinary reconnect', () => {
		const w = watched();
		w.state('RECONNECTING', 'ACTIVE');
		assert.equal(w.recovered(), 0);
		w.lose();
		w.state('UPSTREAM_LOST', 'RECONNECTING');
		w.state('RECONNECTING', 'ACTIVE');
		assert.equal(w.recovered(), 1);
		w.state('RECONNECTING', 'ACTIVE');
		assert.equal(w.recovered(), 1);
	});
});

// conversation-server.ts cannot be imported by a test, so this checks it hands each call's session
// to bodhi's upstreamRecovery and keeps no redial of its own.
describe('conversation-server.ts delegates upstream recovery to bodhi', () => {
	const src = readFileSync(join(import.meta.dirname ?? '.', '..', 'skills/phone-conversation/scripts/conversation-server.ts'), 'utf-8');

	it('configures every call session with phoneUpstreamRecovery and watches it', () => {
		assert.match(src, /\t\tupstreamLossPolicy: 'hold',\n\t\tupstreamRecovery: phoneUpstreamRecovery\(callSession, activeCalls\),\n/);
		assert.match(src, /\twatchPhoneUpstream\(\{\n\t\teventBus: session\.eventBus,/);
	});

	it('keeps no private redial', () => {
		assert.doesNotMatch(src, /createPostParkRedialer|redialUpstream\(|\.recoverUpstream\(/);
	});
});
