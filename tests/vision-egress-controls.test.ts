import { describe, it, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { setTimeout as delay } from 'node:timers/promises';
import {
	setVisionSession,
	setVisionSpeechEvidence,
	submitFrame,
	startStreaming,
	stopStreaming,
	registerSource,
	getVisionEgressStats,
	getVisionState,
	resetVisionEgressForTests,
	VISION_MIN_SEND_INTERVAL_MS,
} from '../src/vision-tools.js';

/** Fake VoiceSession exposing exactly what the vision egress path reads. */
function fakeSession(state = 'ACTIVE') {
	const sent: Array<{ b64: string; mime: string }> = [];
	const session = {
		sessionManager: { state },
		transport: {
			isConnected: true,
			sendFile(b64: string, mime: string) {
				sent.push({ b64, mime });
			},
			sendContent() {},
		},
	};
	return { session, sent };
}

function frameBuf(fill: number, size = 10_000): Buffer {
	return Buffer.alloc(size, fill);
}

beforeEach(() => {
	stopStreaming();
	setVisionSession(null);
	setVisionSpeechEvidence(null);
	resetVisionEgressForTests();
});

describe('P7 D7.4 vision egress controls', () => {
	it('browser push rides the central gate: ACTIVE sends, non-ACTIVE defers to the latest-frame slot', async () => {
		const { session, sent } = fakeSession('CONNECTING');
		setVisionSession(session);
		startStreaming('browser', undefined, 'push');
		const before = getVisionEgressStats();
		const r = submitFrame(frameBuf(1));
		assert.equal(r.ok, true);
		assert.equal(r.deferred, true, 'session not ACTIVE — parked, not sent');
		assert.equal(sent.length, 0);
		assert.equal(getVisionEgressStats().deferredGate, before.deferredGate + 1);
		assert.equal(getVisionEgressStats().slotOccupied, true);
		// The gate opening lets the drain timer deliver the PARKED frame.
		session.sessionManager.state = 'ACTIVE';
		await delay(400);
		assert.equal(sent.length, 1, 'parked frame drained once the gate opened');
		assert.equal(getVisionEgressStats().slotOccupied, false);
		stopStreaming();
	});

	it('pause-during-speech: frames defer while the canonical speech evidence is active', async () => {
		const { session, sent } = fakeSession();
		setVisionSession(session);
		let speaking = true;
		setVisionSpeechEvidence(() => ({ active: speaking }));
		startStreaming('browser', undefined, 'push');
		const r = submitFrame(frameBuf(2));
		assert.equal(r.deferred, true);
		assert.equal(sent.length, 0, 'no vision while the user speaks');
		speaking = false;
		await delay(400);
		assert.equal(sent.length, 1, 'deferred frame followed the speech, not preceded it');
		stopStreaming();
	});

	it('latest-frame-only slot: a burst while gated keeps ONLY the newest frame (no backlog — FE-1 rule)', async () => {
		const { session, sent } = fakeSession();
		setVisionSession(session);
		let speaking = true;
		setVisionSpeechEvidence(() => ({ active: speaking }));
		startStreaming('browser', undefined, 'push');
		const d0 = getVisionEgressStats().displaced;
		submitFrame(frameBuf(0x11));
		submitFrame(frameBuf(0x22));
		submitFrame(frameBuf(0x33));
		assert.equal(getVisionEgressStats().displaced, d0 + 2, 'two frames displaced forever');
		speaking = false;
		await delay(400);
		assert.equal(sent.length, 1, 'exactly one frame — never a drained backlog');
		assert.equal(Buffer.from(sent[0].b64, 'base64')[0], 0x33, 'and it is the NEWEST');
		stopStreaming();
	});

	it('fps cap: a second frame inside the send interval defers on budget', () => {
		const { session, sent } = fakeSession();
		setVisionSession(session);
		startStreaming('browser', undefined, 'push');
		const r1 = submitFrame(frameBuf(1));
		assert.equal(r1.deferred, undefined, 'first frame sends');
		const r2 = submitFrame(frameBuf(2));
		assert.equal(r2.deferred, true, `second frame within ${VISION_MIN_SEND_INTERVAL_MS}ms is over budget`);
		assert.equal(sent.length, 1);
		assert.ok(getVisionEgressStats().deferredBudget >= 1);
		stopStreaming();
	});

	it('stopStream clears the parked frame — stale vision never leaks into the next session', () => {
		const { session, sent } = fakeSession('CONNECTING');
		setVisionSession(session);
		startStreaming('browser', undefined, 'push');
		submitFrame(frameBuf(9));
		assert.equal(getVisionEgressStats().slotOccupied, true);
		stopStreaming();
		assert.equal(getVisionEgressStats().slotOccupied, false);
		assert.equal(sent.length, 0);
	});

	it('wire-byte accounting: an over-burst base64 frame is rejected rather than deferred forever', () => {
		const { session, sent } = fakeSession();
		setVisionSession(session);
		startStreaming('browser', undefined, 'push');
		// 500 KB raw fits the 600 KB burst raw-wise, but its 667 KB wire size
		// cannot ever fit the encoded-byte burst budget.
		const r = submitFrame(frameBuf(1, 500 * 1024));
		assert.equal(r.ok, false);
		assert.equal(r.reason, 'frame-too-large');
		assert.match(r.error ?? '', /vision egress budget/);
		assert.equal(sent.length, 0);
		assert.equal(getVisionEgressStats().droppedOversize, 1);
		assert.equal(getVisionEgressStats().slotOccupied, false);
		stopStreaming();
	});

	it('pull cadence is capped by the egress send interval', () => {
		const { session } = fakeSession();
		setVisionSession(session);
		registerSource({ name: 'fps-cap-source', capture: async () => ({ data: frameBuf(1), mimeType: 'image/jpeg' }) });
		const r = startStreaming('fps-cap-source', 2, 'pull');
		assert.equal(r.status, 'streaming');
		if (r.status === 'streaming') {
			assert.equal(r.intervalMs, VISION_MIN_SEND_INTERVAL_MS);
			assert.equal(r.fps, 1000 / VISION_MIN_SEND_INTERVAL_MS);
		}
		stopStreaming();
	});

	it('stopStreaming fences an in-flight pull capture — the stale frame never sends', async () => {
		const { session, sent } = fakeSession();
		setVisionSession(session);
		let releaseCapture!: () => void;
		registerSource({
			name: 'slowsrc',
			capture: () =>
				new Promise((res) => {
					releaseCapture = () => res({ data: frameBuf(7), mimeType: 'image/jpeg' });
				}),
		});
		startStreaming('slowsrc', 1, 'pull'); // startStream fires an immediate tick
		await delay(10); // the tick is parked inside capture()
		stopStreaming();
		releaseCapture(); // slow source finally returns — AFTER the stop
		await delay(10);
		assert.equal(sent.length, 0, 'stop semantics beat a slow source');
	});

	it('getVisionState exposes the egress diagnostics', () => {
		const st = getVisionState();
		assert.equal(typeof st.egress.sent, 'number');
		assert.equal(typeof st.egress.deferredGate, 'number');
		assert.equal(typeof st.egress.deferredBudget, 'number');
		assert.equal(typeof st.egress.displaced, 'number');
		assert.equal(typeof st.egress.droppedOversize, 'number');
		assert.equal(typeof st.egress.slotOccupied, 'boolean');
	});

	it('§D7.0b budget guard: the residual voice-loop cost of a worst-case 720p frame is bounded', () => {
		// The named residual: base64 + the SDK's synchronous stringify of the
		// frame message. At the downscaled ~150 KB budget this must stay in
		// the low milliseconds.
		const frame = Buffer.alloc(150 * 1024, 0xab);
		const t0 = process.hrtime.bigint();
		const b64 = frame.toString('base64');
		const msg = JSON.stringify({ realtime_input: { video: { data: b64, mimeType: 'image/jpeg' } } });
		const ms = Number(process.hrtime.bigint() - t0) / 1e6;
		assert.ok(msg.length > frame.length);
		assert.ok(ms < 25, `base64+stringify took ${ms.toFixed(1)}ms — downscale budget violated`);
	});
});
