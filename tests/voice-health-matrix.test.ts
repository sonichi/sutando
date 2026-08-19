import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
	evaluateMatrix,
	type MatrixBaseline,
	type MatrixInput,
} from '../src/voice-health-matrix.js';
import type { AudioHealthSnapshot, ClientHeartbeat } from '../src/voice-audio-health.js';

const NOW = 1_000_000;

function hb(over: Partial<ClientHeartbeat> = {}): ClientHeartbeat {
	return {
		nonce: 'n0n0n0n0',
		seq: 10,
		epochAgeMs: 60_000,
		capCallbacks: 47,
		bytesSent: 64000,
		sendSkipped: 0,
		sendFailed: 0,
		chunksRecv: 0,
		chunksScheduled: 0,
		chunksEnded: 0,
		chunksCancelled: 0,
		ctxTimeMs: 30_000,
		scheduledDepth: 0,
		lastEndedAgoMs: null,
		ctxState: 'r',
		captureState: 'o',
		ctxSuspendCount: 0,
		bufferedAmount: 0,
		bufferedHighWater: 0,
		muted: false,
		openGap: null,
		episodeOverflow: 0,
		episodes: [],
		receivedAt: NOW - 1000,
		...over,
	};
}

function snap(over: Partial<AudioHealthSnapshot> = {}): AudioHealthSnapshot {
	return {
		coverage: 'session-only',
		upstream: null,
		transportGeneration: null,
		echoSuppressed: 0,
		logicalSessionId: 1,
		lineageState: 'fresh',
		contextTokens: null,
		contextTokensAt: null,
		contextTokensDetails: null,
		epoch: 12345,
		nonce: 'n0n0n0n0',
		deliveredFrames: 1000,
		deliveredBytes: 1_364_000,
		lastDeliveredAt: NOW - 100,
		egressFrames: 500,
		egressBytes: 100_000,
		lastEgressAt: NOW - 200,
		speech: { active: false, onsetAt: null, lastAboveFloorAt: null },
		ingressRms: 0.001,
		lastHeartbeat: hb(),
		clientTotals: {
			capCallbacks: 1000,
			bytesSent: 1_364_000,
			sendSkipped: 0,
			sendFailed: 0,
			chunksRecv: 500,
			chunksScheduled: 500,
			chunksEnded: 490,
			chunksCancelled: 10,
		},
		heartbeatCount: 20,
		newEpisodeIds: [],
		samplesSkipped: 0,
		lastTurnLatencyMs: null,
		lastSpeechToModelMs: null,
		lastModelEventAt: null,
		inputHealth: 'ok',
		epochStartApproxMs: NOW - 60_000,
		lastMatrixVerdict: null,
		lastMatrixFacts: null,
		lastMatrixReasons: null,
		...over,
	};
}

/** A previous-tick baseline consistent with `snap()` minus the given deltas. */
function upstreamFix(audio: {
	attempted: number;
	queued: number;
	skippedNoSession?: number;
	threw?: number;
	lastQueuedAt?: number | null;
}) {
	const slot = {
		attempted: audio.attempted,
		queued: audio.queued,
		skippedNoSession: audio.skippedNoSession ?? 0,
		threw: audio.threw ?? 0,
		attemptedRawBytes: 0,
		queuedRawBytes: 0,
		attemptedWireBytesEstimate: 0,
		queuedWireBytesEstimate: 0,
		lastAttemptedAt: audio.lastQueuedAt ?? null,
		lastQueuedAt: audio.lastQueuedAt ?? null,
		lastSkippedAt: null,
		lastThrewAt: null,
	};
	return {
		audio: slot,
		video: { ...slot, attempted: 0, queued: 0, skippedNoSession: 0, threw: 0, unsupportedMime: 0 },
		text: { ...slot, attempted: 0, queued: 0, skippedNoSession: 0, threw: 0, skippedEmpty: 0 },
	};
}

function baselineBefore(s: AudioHealthSnapshot, deltas: Partial<MatrixBaseline> = {}): MatrixBaseline {
	return {
		at: NOW - 30_000,
		epoch: s.epoch,
		deliveredFrames: s.deliveredFrames,
		capCallbacks: s.clientTotals.capCallbacks,
		bytesSent: s.clientTotals.bytesSent,
		sendSkipped: s.clientTotals.sendSkipped,
		chunksRecv: s.clientTotals.chunksRecv,
		chunksScheduled: s.clientTotals.chunksScheduled,
		chunksEnded: s.clientTotals.chunksEnded,
		chunksCancelled: s.clientTotals.chunksCancelled,
		transportGeneration: s.transportGeneration,
		audioAttempted: s.upstream?.audio.attempted ?? null,
		audioQueued: s.upstream?.audio.queued ?? null,
		audioSkippedNoSession: s.upstream?.audio.skippedNoSession ?? null,
		audioThrew: s.upstream?.audio.threw ?? null,
		echoSuppressed: s.echoSuppressed,
		ctxTimeMs: s.lastHeartbeat?.ctxTimeMs ?? null,
		bufferedAmount: s.lastHeartbeat?.bufferedAmount ?? null,
		serverBufferedAmount: null,
		maxEpisodeId: 0,
		...deltas,
	};
}

function evalWith(s: AudioHealthSnapshot, prev: MatrixBaseline | null, over: Partial<MatrixInput> = {}) {
	return evaluateMatrix({
		sessionState: 'ACTIVE',
		clientConnected: true,
		snapshot: s,
		prev,
		now: NOW,
		...over,
	});
}

describe('P7 D7.2 matrix — structural outcomes (row 0 + honesty)', () => {
	it('RECONNECTING/CONNECTING → reconnect-window, never a layer verdict', () => {
		for (const st of ['RECONNECTING', 'CONNECTING']) {
			const r = evalWith(snap(), baselineBefore(snap()), { sessionState: st });
			assert.equal(r.verdict, 'reconnect-window');
		}
	});

	it('no client attached → healthy-idle (not an incident)', () => {
		const r = evalWith(snap(), baselineBefore(snap()), { clientConnected: false });
		assert.equal(r.verdict, 'healthy-idle');
		assert.ok(r.reasons.includes('no-client-attached'));
	});

	it('no heartbeat, or no prior baseline → insufficient-evidence (never guess)', () => {
		const r1 = evalWith(snap({ lastHeartbeat: null }), baselineBefore(snap()));
		assert.equal(r1.verdict, 'insufficient-evidence');
		const r2 = evalWith(snap(), null);
		assert.equal(r2.verdict, 'insufficient-evidence');
	});

	it('partial coverage NEVER yields a server-layer verdict: client sends but session delivery stalls', () => {
		// The observable half of rows 2-4's patterns: heartbeats prove PCM
		// crossed the socket, session delivery is flat. At session-only
		// coverage the ingress chain is unobserved — the matrix must say so.
		const s = snap({ lastDeliveredAt: NOW - 20_000 });
		const prev = baselineBefore(s, { capCallbacks: s.clientTotals.capCallbacks - 700 });
		const r = evalWith(s, prev);
		assert.equal(r.verdict, 'insufficient-evidence');
		assert.ok(r.reasons.some((x) => x.includes('server-ingress-chain-unobserved')));
		assert.ok(r.reasons.includes('client-sending-but-session-delivery-stalled'));
	});

	it('speech without a model event at partial coverage → insufficient-evidence naming the blind hop', () => {
		const s = snap({
			speech: { active: true, onsetAt: NOW - 3000, lastAboveFloorAt: NOW - 500 },
		});
		const prev = baselineBefore(s, { deliveredFrames: s.deliveredFrames - 100 });
		const r = evalWith(s, prev, { lastModelEventAt: NOW - 60_000 });
		assert.equal(r.verdict, 'insufficient-evidence');
		assert.ok(r.reasons.some((x) => x.includes('upstream-hop-unobserved')));
	});

	it('speech-silence at session+egress with a LOSSLESS queued-after-speech window → post-sdk-silent', () => {
		const s = snap({
			speech: { active: true, onsetAt: NOW - 3000, lastAboveFloorAt: NOW - 500 },
			upstream: upstreamFix({ attempted: 1100, queued: 1100, lastQueuedAt: NOW - 300 }),
			transportGeneration: 3,
		});
		const prev = baselineBefore(s, {
			deliveredFrames: s.deliveredFrames - 100,
			audioAttempted: 1000,
			audioQueued: 1000,
		});
		const r = evalWith(s, prev, {
			lastModelEventAt: NOW - 60_000,
			effectiveCoverage: 'session+egress',
		});
		assert.equal(r.verdict, 'post-sdk-silent');
		assert.ok(r.reasons.includes('lossless-window'));
	});

	it('a LOSSY window can never be post-sdk-silent — the speech may have been dropped locally', () => {
		const s = snap({
			speech: { active: true, onsetAt: NOW - 3000, lastAboveFloorAt: NOW - 500 },
			// 100 delivered, only 60 attempted: something swallowed frames.
			upstream: upstreamFix({ attempted: 1060, queued: 1060, lastQueuedAt: NOW - 300 }),
			transportGeneration: 3,
		});
		const prev = baselineBefore(s, {
			deliveredFrames: s.deliveredFrames - 100,
			audioAttempted: 1000,
			audioQueued: 1000,
		});
		const r = evalWith(s, prev, {
			lastModelEventAt: NOW - 60_000,
			effectiveCoverage: 'session+egress',
		});
		assert.equal(r.verdict, 'insufficient-evidence');
		assert.ok(r.reasons.includes('lossy-window'));
	});

	it('a transport-generation change inside the window is structural insufficient-evidence', () => {
		const s = snap({
			speech: { active: true, onsetAt: NOW - 3000, lastAboveFloorAt: NOW - 500 },
			upstream: upstreamFix({ attempted: 40, queued: 40, lastQueuedAt: NOW - 300 }),
			transportGeneration: 4, // reconnected mid-window
		});
		const prev = baselineBefore(s, {
			deliveredFrames: s.deliveredFrames - 100,
			transportGeneration: 3,
			audioAttempted: 1000,
			audioQueued: 1000,
		});
		const r = evalWith(s, prev, {
			lastModelEventAt: NOW - 60_000,
			effectiveCoverage: 'session+egress',
		});
		assert.equal(r.verdict, 'insufficient-evidence');
		assert.ok(r.reasons.includes('transport-generation-changed'));
	});

	it('row 4: eligible ingress with ZERO attempts is upstream-send-dead (dead call path)', () => {
		const s = snap({
			upstream: upstreamFix({ attempted: 1000, queued: 1000, lastQueuedAt: NOW - 40_000 }),
			transportGeneration: 3,
		});
		const prev = baselineBefore(s, {
			deliveredFrames: s.deliveredFrames - 100,
			audioAttempted: 1000,
			audioQueued: 1000,
		});
		const r = evalWith(s, prev, { effectiveCoverage: 'session+egress' });
		assert.equal(r.verdict, 'upstream-send-dead');
	});

	it('row 4b: attempts with zero queued is upstream-send-failing, named by guard outcome', () => {
		const s = snap({
			upstream: upstreamFix({
				attempted: 1100,
				queued: 1000,
				skippedNoSession: 100,
				lastQueuedAt: NOW - 40_000,
			}),
			transportGeneration: 3,
		});
		const prev = baselineBefore(s, {
			deliveredFrames: s.deliveredFrames - 100,
			audioAttempted: 1000,
			audioQueued: 1000,
			audioSkippedNoSession: 0, // the 100 skips happened THIS window
		});
		const r = evalWith(s, prev, { effectiveCoverage: 'session+egress' });
		assert.equal(r.verdict, 'upstream-send-failing');
		assert.ok(r.reasons.includes('skippedNoSession+=100'));
		assert.ok(r.reasons.includes('threw+=0'));
		assert.equal(r.facts.attemptedAudioAdvanced, true);
		assert.equal(r.facts.queuedAudioAdvanced, false);
	});

	it('row 4b names the CURRENT window: earlier skips never masquerade as the live failure mode', () => {
		// Generation lifetime: 100 old skips, then 50 throws in THIS window.
		const s = snap({
			upstream: upstreamFix({
				attempted: 1250,
				queued: 1100,
				skippedNoSession: 100,
				threw: 50,
				lastQueuedAt: NOW - 40_000,
			}),
			transportGeneration: 3,
		});
		const prev = baselineBefore(s, {
			deliveredFrames: s.deliveredFrames - 100,
			audioAttempted: 1200,
			audioQueued: 1100,
			audioThrew: 0, // skips predate the window; throws are new
		});
		const r = evalWith(s, prev, { effectiveCoverage: 'session+egress' });
		assert.equal(r.verdict, 'upstream-send-failing');
		assert.ok(r.reasons.includes('skippedNoSession+=0'), 'old skips do not resurface');
		assert.ok(r.reasons.includes('threw+=50'));
	});

	it('echo suppression in the window excludes rows 4/4b — suppression looks like a dead path', () => {
		const s = snap({
			upstream: upstreamFix({ attempted: 1000, queued: 1000, lastQueuedAt: NOW - 40_000 }),
			transportGeneration: 3,
			echoSuppressed: 5,
		});
		const prev = baselineBefore(s, {
			deliveredFrames: s.deliveredFrames - 100,
			audioAttempted: 1000,
			audioQueued: 1000,
			echoSuppressed: 0,
		});
		const r = evalWith(s, prev, { effectiveCoverage: 'session+egress' });
		assert.notEqual(r.verdict, 'upstream-send-dead');
		assert.notEqual(r.verdict, 'upstream-send-failing');
	});

	it('the LIVE evaluator at session-only still downgrades — the shadow rows change nothing', () => {
		const s = snap({
			speech: { active: true, onsetAt: NOW - 3000, lastAboveFloorAt: NOW - 500 },
			upstream: upstreamFix({ attempted: 1100, queued: 1100, lastQueuedAt: NOW - 300 }),
			transportGeneration: 3,
		});
		const prev = baselineBefore(s, {
			deliveredFrames: s.deliveredFrames - 100,
			audioAttempted: 1000,
			audioQueued: 1000,
		});
		const r = evalWith(s, prev, { lastModelEventAt: NOW - 60_000 });
		assert.equal(r.verdict, 'insufficient-evidence');
		assert.ok(r.reasons.some((x) => x.includes('upstream-hop-unobserved')));
		// §1.5: facts are DATA-level observations, identical under every
		// coverage mode — only the VERDICT is coverage-gated (codex round-3 #1).
		const shadow = evalWith(s, prev, {
			lastModelEventAt: NOW - 60_000,
			effectiveCoverage: 'session+egress',
		});
		assert.equal(shadow.verdict, 'post-sdk-silent');
		assert.deepEqual(r.facts, shadow.facts, 'same input, same facts, different claim');
		assert.equal(r.facts.losslessWindowWithSpeechQueued, true);
	});

	it('a baseline with a generation but NO upstream cannot fabricate a lossless window', () => {
		// john-the-dev's finding: the previous tick's observedness was inferred
		// from transportGeneration being non-null, but generation and upstream are
		// independent fields off the same diag. A prev with a REAL generation and
		// absent counters therefore passed the guard, and `?? 0` made the missing
		// counters look like a real zero — so the delta became a lifetime total.
		const s = snap({
			speech: { active: true, onsetAt: NOW - 3000, lastAboveFloorAt: NOW - 500 },
			upstream: upstreamFix({ attempted: 4000, queued: 4000, lastQueuedAt: NOW - 300 }),
			transportGeneration: 5,
		});
		const prev = baselineBefore(s, {
			deliveredFrames: s.deliveredFrames - 100,
			transportGeneration: 5, // NOT null — same generation, so genChanged is false
			audioAttempted: null, // …but upstream was absent at that tick
			audioQueued: null,
			audioSkippedNoSession: null,
			audioThrew: null,
		});
		const r = evalWith(s, prev);
		assert.equal(
			r.facts.losslessWindowWithSpeechQueued,
			false,
			'a window that was never measured must not read as lossless',
		);
		// NOT `r.facts.upstreamWindowObserved` — that is a local const, not a
		// MatrixFacts member, so the assertion could never fail (john-the-dev).
		assert.equal(r.facts.attemptedAudioAdvanced, false);
		assert.equal(r.facts.queuedAudioAdvanced, false);
	});

	it('an unobserved echoSuppressed baseline cannot fabricate echoSuppressedAdvanced', () => {
		// Same defect as the upstream counters, one field over: `?? 0` on a wholly
		// null diag recorded a real zero, so the next observed tick turned a
		// generation-lifetime total into a one-window delta on a fact the
		// watchdog reads (john-the-dev, after 2af342d9 fixed only the siblings).
		const s = snap({
			speech: { active: true, onsetAt: NOW - 3000, lastAboveFloorAt: NOW - 500 },
			upstream: upstreamFix({ attempted: 900, queued: 900, lastQueuedAt: NOW - 300 }),
			transportGeneration: 5,
			echoSuppressed: 4000,
		});
		const prev = baselineBefore(s, {
			deliveredFrames: s.deliveredFrames - 100,
			transportGeneration: 5,
			echoSuppressed: null, // the tick where diagnostics were absent entirely
		});
		const r = evalWith(s, prev);
		assert.equal(
			r.facts.echoSuppressedAdvanced,
			false,
			'a lifetime total across an unobserved baseline is not a window delta',
		);
	});

	it('an unobserved BASELINE can never fabricate post-sdk-silent (null→observed transition)', () => {
		// codex round-5 #3 repro: diagnostics were unobserved at the previous
		// tick (generation null, counters placeholder zeros); observed now with
		// generation-lifetime totals. Diffing would fabricate a lossless window.
		const s = snap({
			speech: { active: true, onsetAt: NOW - 3000, lastAboveFloorAt: NOW - 500 },
			upstream: upstreamFix({ attempted: 900, queued: 900, lastQueuedAt: NOW - 300 }),
			transportGeneration: 3,
		});
		const prev = baselineBefore(s, {
			deliveredFrames: s.deliveredFrames - 100,
			transportGeneration: null,
			audioAttempted: 0,
			audioQueued: 0,
			audioSkippedNoSession: 0,
			audioThrew: 0,
		});
		const r = evalWith(s, prev, {
			lastModelEventAt: NOW - 60_000,
			effectiveCoverage: 'session+egress',
		});
		assert.equal(r.verdict, 'insufficient-evidence');
		assert.ok(r.reasons.includes('upstream-window-unobserved'));
		assert.equal(r.facts.losslessWindowWithSpeechQueued, false);

		// The OTHER direction (codex round-3 #2): counters present but the
		// CURRENT generation unobserved — the window is equally invalid.
		const s2 = snap({
			speech: { active: true, onsetAt: NOW - 3000, lastAboveFloorAt: NOW - 500 },
			upstream: upstreamFix({ attempted: 900, queued: 900, lastQueuedAt: NOW - 300 }),
			transportGeneration: null,
		});
		const prev2 = baselineBefore(s2, {
			deliveredFrames: s2.deliveredFrames - 100,
			transportGeneration: 3,
			audioAttempted: 100,
			audioQueued: 100,
		});
		const r2 = evalWith(s2, prev2, {
			lastModelEventAt: NOW - 60_000,
			effectiveCoverage: 'session+egress',
		});
		assert.equal(r2.verdict, 'insufficient-evidence');
		assert.ok(r2.reasons.includes('upstream-window-unobserved'));
		assert.equal(r2.facts.attemptedAudioAdvanced, false);
	});

	it('rows 4/4b need a valid window too — an unobserved baseline cannot prove a dead path', () => {
		const s = snap({
			upstream: upstreamFix({ attempted: 0, queued: 0 }),
			transportGeneration: 3,
		});
		const prev = baselineBefore(s, {
			deliveredFrames: s.deliveredFrames - 100,
			transportGeneration: null,
		});
		const r = evalWith(s, prev, { effectiveCoverage: 'session+egress' });
		assert.notEqual(r.verdict, 'upstream-send-dead');
		assert.notEqual(r.verdict, 'upstream-send-failing');
	});

	it('facts (design §1.5): the recorded decision carries the upstream observations', () => {
		const s = snap({
			speech: { active: true, onsetAt: NOW - 3000, lastAboveFloorAt: NOW - 500 },
			upstream: upstreamFix({ attempted: 1100, queued: 1100, lastQueuedAt: NOW - 300 }),
			transportGeneration: 3,
		});
		const prev = baselineBefore(s, {
			deliveredFrames: s.deliveredFrames - 100,
			audioAttempted: 1000,
			audioQueued: 1000,
		});
		const r = evalWith(s, prev, {
			lastModelEventAt: NOW - 60_000,
			effectiveCoverage: 'session+egress',
		});
		assert.equal(r.verdict, 'post-sdk-silent');
		assert.deepEqual(r.facts, {
			attemptedAudioAdvanced: true,
			queuedAudioAdvanced: true,
			losslessWindowWithSpeechQueued: true,
			transportGenerationChanged: false,
			echoSuppressedAdvanced: false,
			// ACTIVE-silence family: this window HAS a same-epoch baseline, so
			// factsAvailable holds and speech/ingress report what they observed.
			factsAvailable: true,
			speechInWindow: true,
			speechObservedAt: r.facts.speechObservedAt,
			ingressAdvanced: true,
			modelSilentFor15s: true,
		});
		assert.equal(typeof r.facts.speechObservedAt, 'number');
	});

	it('facts: a generation change is a fact; a windowless tick carries all-false facts', () => {
		const s = snap({
			upstream: upstreamFix({ attempted: 40, queued: 40 }),
			transportGeneration: 4,
		});
		const prev = baselineBefore(s, { transportGeneration: 3 });
		const r = evalWith(s, prev, { effectiveCoverage: 'session+egress' });
		assert.equal(r.verdict, 'insufficient-evidence');
		assert.equal(r.facts.transportGenerationChanged, true);
		assert.equal(r.facts.attemptedAudioAdvanced, false);
		// The fact is coverage-independent: the live session-only evaluator
		// records the same generation change (its verdict path just differs).
		const live = evalWith(s, prev);
		assert.deepEqual(live.facts, r.facts);
		const first = evalWith(snap(), null);
		assert.deepEqual(first.facts, {
			attemptedAudioAdvanced: false,
			queuedAudioAdvanced: false,
			losslessWindowWithSpeechQueued: false,
			transportGenerationChanged: false,
			echoSuppressedAdvanced: false,
			// No baseline ⇒ the ACTIVE-silence family fails closed as a unit.
			factsAvailable: false,
			speechInWindow: false,
			speechObservedAt: null,
			ingressAdvanced: false,
			modelSilentFor15s: false,
		});
	});

	it('facts are verdict-independent: an early return carries the same data-level facts', () => {
		// Identical diagnostics, one tick evaluated ACTIVE and one CONNECTING:
		// the verdicts differ (reconnect-window wins row 0) but the facts are
		// the SAME set — the watchdog must behave identically (§1.5).
		const s = snap({
			upstream: upstreamFix({ attempted: 1100, queued: 1100, lastQueuedAt: NOW - 300 }),
			transportGeneration: 3,
			speech: { active: true, onsetAt: NOW - 3000, lastAboveFloorAt: NOW - 500 },
		});
		const prev = baselineBefore(s, {
			deliveredFrames: s.deliveredFrames - 100,
			audioAttempted: 1000,
			audioQueued: 1000,
		});
		const active = evalWith(s, prev, {
			lastModelEventAt: NOW - 60_000,
			effectiveCoverage: 'session+egress',
		});
		const connecting = evalWith(s, prev, {
			sessionState: 'CONNECTING',
			lastModelEventAt: NOW - 60_000,
			effectiveCoverage: 'session+egress',
		});
		assert.equal(active.verdict, 'post-sdk-silent');
		assert.equal(connecting.verdict, 'reconnect-window');
		assert.deepEqual(connecting.facts, active.facts);
	});
});

describe('P7 D7.2 matrix — row 1 client capture dead', () => {
	it('an OPEN gap overlapping the ingress stall while unmuted → client-capture-dead', () => {
		const s = snap({
			lastDeliveredAt: NOW - 10_000,
			lastHeartbeat: hb({ openGap: { startMs: 48_000, ageMs: 11_000 }, ctxState: 's' }),
		});
		const r = evalWith(s, baselineBefore(s));
		assert.equal(r.verdict, 'client-capture-dead');
		assert.ok(r.reasons.some((x) => x.startsWith('open-gap')));
		assert.ok(r.reasons.includes('shape=ctx-suspended'), 'S2 discriminator from ctxState');
	});

	it('a fresh CLOSED gap episode overlapping the stall → client-capture-dead (main-thread shape)', () => {
		// Gap ended 4 s ago (epoch-relative 55s..56.2s with epoch start NOW-60s),
		// ingress stalled since 6 s ago — the intervals overlap.
		const s = snap({
			lastDeliveredAt: NOW - 6_000,
			lastHeartbeat: hb({ episodes: [{ id: 3, kind: 'gap', startMs: 55_000, durationMs: 1_200 }] }),
		});
		const r = evalWith(s, baselineBefore(s));
		assert.equal(r.verdict, 'client-capture-dead');
		assert.ok(r.reasons.includes('shape=main-thread-gap'));
	});

	it('an OLD latched gap must not pair with unrelated later silence (round-3 #3)', () => {
		// Episode ended ~55 s ago (expired against the 90 s freshness bound is
		// not enough — it also ends long before the stall started 3 s ago).
		const s = snap({
			lastDeliveredAt: NOW - 5_500,
			epochStartApproxMs: NOW - 300_000,
			lastHeartbeat: hb({ episodes: [{ id: 1, kind: 'gap', startMs: 5_000, durationMs: 1_000 }] }),
		});
		const r = evalWith(s, baselineBefore(s));
		assert.notEqual(r.verdict, 'client-capture-dead');
	});

	it('muted suppresses row 1 (mute is not a capture failure)', () => {
		const s = snap({
			lastDeliveredAt: NOW - 10_000,
			lastHeartbeat: hb({ muted: true, openGap: { startMs: 48_000, ageMs: 11_000 } }),
		});
		const r = evalWith(s, baselineBefore(s));
		assert.notEqual(r.verdict, 'client-capture-dead');
	});
});

describe('P7 D7.2 matrix — rows 6/7a/7b', () => {
	it('row 6: growing client bufferedAmount → client-send-backpressure', () => {
		const s = snap({ lastHeartbeat: hb({ bufferedAmount: 300_000 }) });
		const prev = baselineBefore(s, { bufferedAmount: 50_000 });
		const r = evalWith(s, prev);
		assert.equal(r.verdict, 'client-send-backpressure');
	});

	it('row 6: a sendSkipped delta alone is transmitted evidence (round-3 #5)', () => {
		const s = snap({
			clientTotals: { ...snap().clientTotals, sendSkipped: 12 },
		});
		const prev = baselineBefore(s, { sendSkipped: 0 });
		const r = evalWith(s, prev);
		assert.equal(r.verdict, 'client-send-backpressure');
	});

	it('row 7a: server egress buffered growing while client chunksRecv stalls', () => {
		const s = snap();
		const prev = baselineBefore(s, { serverBufferedAmount: 10_000 });
		const r = evalWith(s, prev, { serverBufferedAmount: 500_000 });
		assert.equal(r.verdict, 'egress-backpressure');
	});

	it('row 7a needs the server buffered input — absent ⇒ no verdict from that pattern', () => {
		const s = snap();
		const prev = baselineBefore(s, {
			deliveredFrames: s.deliveredFrames - 700,
			capCallbacks: s.clientTotals.capCallbacks - 700,
		});
		const r = evalWith(s, prev, { serverBufferedAmount: null });
		assert.equal(r.verdict, 'healthy-idle');
	});

	it('row 7b: chunks scheduled but zero NATURAL completions and a frozen ctx clock → playback failure', () => {
		const s = snap({ lastHeartbeat: hb({ ctxTimeMs: 30_000 }) });
		const prev = baselineBefore(s, {
			chunksScheduled: s.clientTotals.chunksScheduled - 20,
			ctxTimeMs: 30_000, // clock frozen
		});
		const r = evalWith(s, prev);
		assert.equal(r.verdict, 'client-playback-failure');
	});

	it('row 7b does not fire while the ctx clock advances (chunks may still be draining)', () => {
		const s = snap({ lastHeartbeat: hb({ ctxTimeMs: 60_000 }) });
		const prev = baselineBefore(s, {
			chunksScheduled: s.clientTotals.chunksScheduled - 20,
			ctxTimeMs: 30_000,
		});
		const r = evalWith(s, prev);
		assert.notEqual(r.verdict, 'client-playback-failure');
	});

	it('row 7b: cancellations are excluded by construction (barge-in flush ≠ playback failure)', () => {
		// 20 scheduled, 20 CANCELLED (chunksEnded unchanged) — but the ctx
		// clock advanced: a barge-in, not a dead output path.
		const s = snap({
			lastHeartbeat: hb({ ctxTimeMs: 45_000 }),
			clientTotals: { ...snap().clientTotals, chunksCancelled: 30 },
		});
		const prev = baselineBefore(s, {
			chunksScheduled: s.clientTotals.chunksScheduled - 20,
			ctxTimeMs: 30_000,
		});
		assert.notEqual(evalWith(s, prev).verdict, 'client-playback-failure');
	});
});

describe('P7 D7.2 matrix — row 5′ healthy-idle', () => {
	it('quiet user, everything advancing → healthy-idle (not an incident)', () => {
		const s = snap();
		const prev = baselineBefore(s, {
			deliveredFrames: s.deliveredFrames - 700,
			capCallbacks: s.clientTotals.capCallbacks - 700,
		});
		const r = evalWith(s, prev);
		assert.equal(r.verdict, 'healthy-idle');
	});
});

describe('P7 D7.2 matrix — round-3 honesty + ordering fixes', () => {
	it('an epoch boundary between ticks is never diffed — insufficient-evidence', () => {
		const s = snap();
		const prev = baselineBefore(s, { epoch: 99, deliveredFrames: s.deliveredFrames + 5000 });
		const r = evalWith(s, prev);
		assert.equal(r.verdict, 'insufficient-evidence');
		assert.ok(r.reasons.includes('epoch-boundary'));
		assert.equal(r.baseline.epoch, s.epoch, 'fresh baseline adopts the new epoch');
	});

	it('episode overflow means the quiet window cannot be proven quiet', () => {
		const s = snap({ lastHeartbeat: hb({ episodeOverflow: 2 }) });
		const prev = baselineBefore(s, {
			deliveredFrames: s.deliveredFrames - 700,
			capCallbacks: s.clientTotals.capCallbacks - 700,
		});
		const r = evalWith(s, prev);
		assert.equal(r.verdict, 'insufficient-evidence');
		assert.ok(r.reasons.some((x) => x.startsWith('episodeOverflow')));
	});

	it('a stale heartbeat cannot support healthy-idle', () => {
		const s = snap({ lastHeartbeat: hb({ receivedAt: NOW - 20_000 }) });
		const prev = baselineBefore(s, {
			deliveredFrames: s.deliveredFrames - 700,
			capCallbacks: s.clientTotals.capCallbacks - 700,
		});
		const r = evalWith(s, prev);
		assert.equal(r.verdict, 'insufficient-evidence');
		assert.ok(r.reasons.includes('heartbeat-stale'));
	});

	it('zero progress while unmuted is missing telemetry, not health', () => {
		const s = snap({ lastDeliveredAt: NOW - 1000 });
		const r = evalWith(s, baselineBefore(s));
		assert.equal(r.verdict, 'insufficient-evidence');
		assert.ok(r.reasons.includes('no-progress-in-window'));
	});

	it('row 5 outranks row 6: speech with no model event wins over backpressure (D7.2 order)', () => {
		const s = snap({
			speech: { active: true, onsetAt: NOW - 3000, lastAboveFloorAt: NOW - 500 },
			lastHeartbeat: hb({ bufferedAmount: 500_000 }),
			upstream: upstreamFix({ attempted: 1100, queued: 1100, lastQueuedAt: NOW - 300 }),
			transportGeneration: 3,
		});
		const prev = baselineBefore(s, {
			deliveredFrames: s.deliveredFrames - 100,
			bufferedAmount: 50_000,
			audioAttempted: 1000,
			audioQueued: 1000,
		});
		const r = evalWith(s, prev, {
			lastModelEventAt: NOW - 60_000,
			effectiveCoverage: 'session+egress',
		});
		assert.equal(r.verdict, 'post-sdk-silent');
	});

	it('a stale client speech EPISODE alone is not current speech evidence (canonical tracker only)', () => {
		const s = snap({
			speech: { active: false, onsetAt: null, lastAboveFloorAt: null },
			lastHeartbeat: hb({ episodes: [{ id: 7, kind: 'speech', onsetSeq: 5, offsetSeq: 9, maxRmsPm: 300, aboveFloorMs: 900 }] }),
		});
		const prev = baselineBefore(s, {
			deliveredFrames: s.deliveredFrames - 700,
			capCallbacks: s.clientTotals.capCallbacks - 700,
		});
		const r = evalWith(s, prev, { lastModelEventAt: NOW - 60_000 });
		assert.notEqual(r.verdict, 'post-sdk-silent');
		assert.ok(!r.reasons.includes('speech-without-model-event'));
	});

	it('row 7b: an all-cancelled window (barge-in flush) proves nothing about playback', () => {
		const s = snap({ lastHeartbeat: hb({ ctxTimeMs: 30_000 }) });
		const prev = baselineBefore(s, {
			chunksScheduled: s.clientTotals.chunksScheduled - 20,
			chunksCancelled: s.clientTotals.chunksCancelled - 20,
			deliveredFrames: s.deliveredFrames - 700,
			capCallbacks: s.clientTotals.capCallbacks - 700,
			ctxTimeMs: 30_000,
		});
		const r = evalWith(s, prev);
		assert.notEqual(r.verdict, 'client-playback-failure');
	});
});

// ═════════════════════════════════════════════════════════════════════════════
// HYPOTHESIS FIXTURES (D7.2, round-1 #9): the FE-1/FE-2 incident counters
// never existed — these encode the *hypothesized* field patterns from
// docs/investigation-voice-incident-2026-08-09.md and must be validated
// against captured S1/S2 runs (D7.6) before being treated as ground truth.
// FE-3 is exercised through D7.6-S3 continuity scenarios, not the matrix.
// ═════════════════════════════════════════════════════════════════════════════

describe('P7 D7.2 hypothesis fixtures (NOT ground truth until S-runs validate)', () => {
	it('FE-1 [HYPOTHESIS]: vision saturation — client egress backpressure → row 6', () => {
		// Hypothesized: 557 native-res frames saturate the shared socket;
		// bufferedAmount climbs and PCM frames start skipping.
		const s = snap({
			lastHeartbeat: hb({ bufferedAmount: 2_500_000, bufferedHighWater: 2_500_000 }),
			clientTotals: { ...snap().clientTotals, sendSkipped: 40 },
		});
		const prev = baselineBefore(s, { bufferedAmount: 400_000, sendSkipped: 0 });
		const r = evalWith(s, prev);
		assert.equal(r.verdict, 'client-send-backpressure');
	});

	it('FE-2 [HYPOTHESIS]: shared AudioContext death mid-word — suspension gap → row 1', () => {
		// Hypothesized: the context suspends mid-utterance; capture callbacks
		// stop; the open gap travels while ingress goes quiet, socket healthy.
		const s = snap({
			lastDeliveredAt: NOW - 12_000,
			lastHeartbeat: hb({
				ctxState: 's',
				ctxSuspendCount: 1,
				openGap: { startMs: 47_000, ageMs: 12_500 },
			}),
		});
		const r = evalWith(s, baselineBefore(s));
		assert.equal(r.verdict, 'client-capture-dead');
		assert.ok(r.reasons.includes('shape=ctx-suspended'));
	});
});
