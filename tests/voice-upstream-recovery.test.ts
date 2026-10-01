import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { VoiceSession, type RecoverUpstreamArgs, type VoiceSessionConfig } from 'bodhi-realtime-agent';
import {
	createPostParkRedialer,
	createUpstreamRedialer,
	onConnectingTick,
	redialUpstream,
	replaceHungDial,
	type RecoverySurface,
} from '../src/voice-upstream-recovery.js';

const quiet = { log: () => {}, error: () => {} };

function fake(state: string, canRecover = true) {
	const calls = { recover: [] as RecoverUpstreamArgs[], legacy: 0, closed: 0 };
	const s: RecoverySurface = {
		sessionManager: { state, transitionTo: (to) => { if (to === 'CLOSED') calls.closed++; } },
		getRecoveryCapabilities: () => ({ recoverUpstream: canRecover }),
		recoverUpstream: (args) => { calls.recover.push(args); return { activated: Promise.resolve() }; },
		handleClientConnected: () => { calls.legacy++; },
	};
	return { s, calls };
}

describe('redialUpstream', () => {
	it('redials a parked session through recoverUpstream with the full-context arguments', () => {
		const { s, calls } = fake('UPSTREAM_LOST');
		const path = redialUpstream(s, { origin: 'Health', reason: 'human-retry', legacy: (d) => d(), ...quiet });
		assert.equal(path, 'recover');
		assert.deepEqual(calls.recover, [{ reason: 'human-retry', skipContextInjection: false, holdSyntheticUntilFreshSpeech: false }]);
		assert.equal(calls.legacy, 0);
	});

	it('passes the caller reason through', () => {
		const { s, calls } = fake('UPSTREAM_LOST');
		redialUpstream(s, { origin: 'Redial', reason: 'fatal-backoff-clear', legacy: (d) => d(), ...quiet });
		assert.equal(calls.recover[0]?.reason, 'fatal-backoff-clear');
	});

	it('runs the legacy reconnect from CLOSED, inside the caller wrapper', () => {
		const { s, calls } = fake('CLOSED');
		let wrapped = 0;
		const path = redialUpstream(s, { origin: 'Health', reason: 'human-retry', legacy: (d) => { wrapped++; d(); }, ...quiet });
		assert.equal(path, 'legacy');
		assert.equal(wrapped, 1);
		assert.equal(calls.legacy, 1);
		assert.equal(calls.recover.length, 0);
	});

	it('falls back to the legacy reconnect when the runtime cannot recover', () => {
		const { s, calls } = fake('UPSTREAM_LOST', false);
		assert.equal(redialUpstream(s, { origin: 'Health', reason: 'human-retry', legacy: (d) => d(), ...quiet }), 'legacy');
		assert.equal(calls.recover.length, 0);
		assert.equal(calls.legacy, 1);
	});

	it('does nothing without a session', () => {
		assert.equal(redialUpstream(null, { origin: 'Health', reason: 'human-retry', legacy: () => assert.fail('dialed'), ...quiet }), 'none');
	});

	it('a throwing recoverUpstream is reported, not raised', () => {
		const { s } = fake('UPSTREAM_LOST');
		s.recoverUpstream = () => { throw new Error('boom'); };
		const errors: string[] = [];
		assert.doesNotThrow(() => redialUpstream(s, { origin: 'Health', reason: 'human-retry', legacy: (d) => d(), log: () => {}, error: (m) => { errors.push(m); } }));
		assert.match(errors.join('\n'), /recoverUpstream threw/);
	});
});

describe('replaceHungDial', () => {
	it('replaces the hung dial with recoverUpstream and leaves the state alone', () => {
		const { s, calls } = fake('CONNECTING');
		assert.equal(replaceHungDial(s, 90, quiet), true);
		assert.equal(calls.recover.length, 1);
		assert.equal(calls.closed, 0);
	});

	it('forces CLOSED when the runtime cannot recover', () => {
		const { s, calls } = fake('CONNECTING', false);
		assert.equal(replaceHungDial(s, 90, quiet), true);
		assert.equal(calls.recover.length, 0);
		assert.equal(calls.closed, 1);
	});

	it('reports a failed recovery so the caller keeps the clock armed', () => {
		const { s } = fake('CONNECTING');
		s.recoverUpstream = () => { throw new Error('boom'); };
		assert.equal(replaceHungDial(s, 90, quiet), false);
	});
});

describe('the installed bodhi VoiceSession', () => {
	it('exposes the recovery surface these helpers call, under upstreamLossPolicy hold', () => {
		const session = new VoiceSession({
			sessionId: 's', userId: 'u', apiKey: 'unused', port: 0,
			agents: [{ name: 'main', instructions: 'x', tools: [] }], initialAgent: 'main',
			geminiModel: 'unused', upstreamLossPolicy: 'hold', log: () => {},
		} as unknown as VoiceSessionConfig);
		const surface: RecoverySurface = session as unknown as RecoverySurface;
		assert.equal(typeof surface.recoverUpstream, 'function');
		assert.equal(surface.getRecoveryCapabilities?.()?.recoverUpstream, true);
	});
});

// Delegation: each host's decision point lives in a function here, so a mutation that stops it
// dialling fails a test instead of passing unseen at a one-line call site.
describe('host delegation', () => {
	it('createUpstreamRedialer resolves the session on every call and dials it', () => {
		const { s, calls } = fake('UPSTREAM_LOST');
		const dial = createUpstreamRedialer({ getSession: () => s, legacy: (d) => d(), ...quiet });
		assert.equal(dial('Redial'), 'recover');
		assert.equal(calls.recover.length, 1);
		assert.equal(calls.recover[0]?.reason, 'human-retry');
		assert.equal(dial('Health', 'fatal-backoff-clear'), 'recover');
		assert.equal(calls.recover[1]?.reason, 'fatal-backoff-clear');
	});

	it('createUpstreamRedialer reports none, and never dials, when no session is attached', () => {
		const dial = createUpstreamRedialer({ getSession: () => null, legacy: () => { throw new Error('dialled'); }, ...quiet });
		assert.equal(dial('Redial'), 'none');
	});

	it('onConnectingTick replaces the hung dial only when the tick forces a close', () => {
		const stuck = fake('CONNECTING');
		assert.equal(onConnectingTick({ forceClose: true, session: stuck.s, stuckForS: 42, ...quiet }), true);
		assert.equal(stuck.calls.recover.length, 1);

		const quiet2 = fake('CONNECTING');
		assert.equal(onConnectingTick({ forceClose: false, session: quiet2.s, stuckForS: 42, ...quiet }), false);
		assert.equal(quiet2.calls.recover.length, 0);
		assert.equal(quiet2.calls.closed, 0);
	});

	it('createPostParkRedialer dials a parked session, holding synthetic output until fresh speech', () => {
		const { s, calls } = fake('UPSTREAM_LOST');
		const redial = createPostParkRedialer({ getSession: () => s, isLive: () => true, origin: 'Phone CA1', hold: true, ...quiet });
		assert.equal(redial(), 'recover');
		assert.deepEqual(calls.recover, [{ reason: 'human-retry', skipContextInjection: false, holdSyntheticUntilFreshSpeech: true }]);
	});

	it('createPostParkRedialer skips a call that is gone, and leaves an unparked one alone', () => {
		const gone = fake('UPSTREAM_LOST');
		const a = createPostParkRedialer({ getSession: () => gone.s, isLive: () => false, origin: 'Phone CA2', hold: true, ...quiet });
		assert.equal(a(), 'skipped');
		assert.equal(gone.calls.recover.length, 0);

		// No legacy path on this adapter: a session that is not parked must not be touched at all.
		const live = fake('CLOSED');
		const b = createPostParkRedialer({ getSession: () => live.s, isLive: () => true, origin: 'Phone CA3', hold: true, ...quiet });
		assert.equal(b(), 'none');
		assert.equal(live.calls.recover.length, 0);
		assert.equal(live.calls.legacy, 0);
	});

	it('createPostParkRedialer runs onActivated once the dial activates', async () => {
		const { s } = fake('UPSTREAM_LOST');
		let activated = 0;
		const redial = createPostParkRedialer({
			getSession: () => s, isLive: () => true, origin: 'Phone CA4', hold: true,
			onActivated: () => { activated++; }, ...quiet,
		});
		assert.equal(redial(), 'recover');
		await new Promise((r) => setImmediate(r));
		assert.equal(activated, 1);
	});
});
