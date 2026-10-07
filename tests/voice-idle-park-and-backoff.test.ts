// The idle teardown and the fatal-close backoff, run against the installed bodhi VoiceSession with
// only the Gemini SDK's connect faked, so the engine's own reconnector is the thing under test.
import { after, describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { Live } from '@google/genai';
import { VoiceSession, type VoiceSessionConfig } from 'bodhi-realtime-agent';
import { hostOwnsUpstreamRecovery, parkIdleUpstream } from '../src/voice-upstream-recovery.js';

const quiet = { log: () => {}, error: () => {} };
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

type Callbacks = {
	onopen?: () => void;
	onmessage: (msg: unknown) => void;
	onclose?: (e: { code: number; reason: string }) => void;
};
type TestSession = {
	start(): Promise<void>;
	close?(): Promise<void>;
	parkUpstream(reason: string): Promise<void>;
	clientConnected?: boolean;
	sessionManager: { state: unknown };
	transport: { disconnect(): Promise<void> };
};
const liveProto = Live.prototype as unknown as { connect: (p: { callbacks: Callbacks }) => Promise<unknown> };

let dials = 0;
let cbs!: Callbacks;
const realConnect = liveProto.connect;
liveProto.connect = async function (params) {
	dials++;
	cbs = params.callbacks;
	const my = cbs;
	setTimeout(() => { my.onopen?.(); my.onmessage({ setupComplete: {} }); }, 5);
	return {
		sendRealtimeInput() {}, sendClientContent() {}, sendToolResponse() {},
		close() { setTimeout(() => my.onclose?.({ code: 1000, reason: '' }), 5); },
	};
};
after(() => { liveProto.connect = realConnect; });

const sessions: TestSession[] = [];
after(async () => { for (const s of sessions) await Promise.resolve(s.close?.()).catch(() => {}); });

async function activeSession(suppress: () => boolean = () => false) {
	dials = 0;
	const s = new VoiceSession({
		sessionId: `s${sessions.length}`, userId: 'u', apiKey: 'unused', port: 0,
		agents: [{ name: 'main', instructions: 'x', tools: [] }], initialAgent: 'main',
		geminiModel: 'gemini-3.1-flash-live-preview', upstreamLossPolicy: 'hold',
		suppressClientAutoActions: suppress, log: () => {},
	} as unknown as VoiceSessionConfig) as unknown as TestSession;
	sessions.push(s);
	await s.start();
	for (let i = 0; i < 100 && String(s.sessionManager.state) !== 'ACTIVE'; i++) await sleep(10);
	assert.equal(String(s.sessionManager.state), 'ACTIVE');
	cbs.onmessage({ sessionResumptionUpdate: { newHandle: 'h-A', resumable: true } });
	return s;
}
const state = (s: TestSession) => String(s.sessionManager.state);

describe('idle teardown on bodhi 0.4', () => {
	it('control: closing the transport makes the engine resume the idle session itself', async () => {
		const s = await activeSession();
		await s.transport.disconnect();
		await sleep(1500);
		assert.equal(state(s), 'ACTIVE');
		assert.equal(dials, 2, 'the engine redialled with no client attached');
	});

	it('parkIdleUpstream rests in UPSTREAM_LOST with no further dial, even across a GoAway', async () => {
		const s = await activeSession();
		assert.equal(await parkIdleUpstream(s, 'idle', quiet), 'parked');
		await sleep(1500);
		assert.equal(state(s), 'UPSTREAM_LOST');
		assert.equal(dials, 1);
		cbs.onmessage({ goAway: { timeLeft: '1s' } });
		await sleep(1500);
		assert.equal(dials, 1, 'a GoAway on a parked session dialled');
	});

	it('parkIdleUpstream leaves a session with an attached client alone', async () => {
		assert.equal(await parkIdleUpstream({ clientConnected: true, parkUpstream: async () => { throw new Error('parked'); } }, 'idle', quiet), 'attached');
	});
});

describe('fatal-close backoff gates the engine reconnector', () => {
	async function fatalClose(withGate: boolean) {
		let backoffUntil = 0;
		let s: TestSession | null = null;
		s = await activeSession(() => hostOwnsUpstreamRecovery({
			coordinatorOwns: false, state: s?.sessionManager?.state, now: Date.now(),
			fatalBackoffUntil: withGate ? backoffUntil : 0,
		}));
		cbs.onclose({ code: 1011, reason: 'You exceeded your current quota' });
		backoffUntil = Date.now() + 5 * 60 * 1000;   // the host's classifier sets it after the engine's close handler
		await sleep(1600);
		return s;
	}

	it('control: without the backoff term the engine redials a fatal close', async () => {
		await fatalClose(false);
		assert.ok(dials > 1, `expected an engine redial, saw ${dials} dial(s)`);
	});

	it('with the backoff term the engine parks instead of redialling', async () => {
		const s = await fatalClose(true);
		assert.equal(dials, 1);
		assert.equal(state(s), 'UPSTREAM_LOST');
	});

	it('the term applies only while the engine is reconnecting, so a client attach still dials', () => {
		const backoff = { coordinatorOwns: false, now: 1000, fatalBackoffUntil: 2000 };
		assert.equal(hostOwnsUpstreamRecovery({ ...backoff, state: 'RECONNECTING' }), true);
		assert.equal(hostOwnsUpstreamRecovery({ ...backoff, state: 'UPSTREAM_LOST' }), false);
		assert.equal(hostOwnsUpstreamRecovery({ ...backoff, state: 'ACTIVE' }), false);
		assert.equal(hostOwnsUpstreamRecovery({ ...backoff, now: 3000, state: 'RECONNECTING' }), false);
		assert.equal(hostOwnsUpstreamRecovery({ ...backoff, coordinatorOwns: true, state: 'ACTIVE' }), true);
	});
});
