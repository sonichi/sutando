/**
 * MatrixResult.facts — the verdict-independent raw facts the ACTIVE-silence
 * watchdog consumes (design-voice-active-silence-recovery.md §Trigger (a),
 * residue item 1). Facts must be identical across coverage modes and verdicts,
 * fail closed when their inputs are missing, and name their formulas:
 *   ingressAdvanced  = delta(deliveredFrames) > 0        (session-only)
 *   modelSilentFor15s = lastModelEventAt !== null && now - lastModelEventAt > 15_000
 *   speechObservedAt  = onset (or last above-floor) iff speechInWindow
 *
 * Imports the real evaluateMatrix rather than restating its rules.
 */
import { evaluateMatrix, type MatrixBaseline } from '../src/voice-health-matrix.js';
import type { AudioHealthSnapshot } from '../src/voice-audio-health.js';

let failed = 0;
function check(name: string, got: unknown, want: unknown): void {
	const ok = JSON.stringify(got) === JSON.stringify(want);
	console.log(`  ${ok ? 'ok  ' : 'FAIL'} ${name}`);
	if (!ok) { console.log(`       got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`); failed++; }
}

const NOW = 10_000_000;

function snapshot(o: Partial<AudioHealthSnapshot> & {
	deliveredFrames?: number; lastModelEventAt?: number | null;
} = {}): AudioHealthSnapshot {
	return {
		coverage: 'session-only',
		epoch: 111,
		nonce: 'n1',
		deliveredFrames: o.deliveredFrames ?? 1_000,
		deliveredBytes: 0,
		lastDeliveredAt: NOW - 100,
		egressFrames: 0,
		egressBytes: 0,
		lastEgressAt: null,
		speech: (o as { speech?: AudioHealthSnapshot['speech'] }).speech
			?? { active: false, onsetAt: null, lastAboveFloorAt: NOW - 5_000 },
		ingressRms: 0,
		lastHeartbeat: (o as { lastHeartbeat?: AudioHealthSnapshot['lastHeartbeat'] }).lastHeartbeat
			?? ({
				receivedAt: NOW - 1_000, capCallbacks: 30, bytesSent: 1, sendSkipped: 0,
				chunksRecv: 0, chunksScheduled: 0, chunksEnded: 0, chunksCancelled: 0,
				ctxTimeMs: 1, bufferedAmount: 0, muted: false, openGap: null, episodes: [],
				episodeOverflow: 0, ctxState: 'r', captureState: 'o', ctxSuspendCount: 0,
			} as unknown as AudioHealthSnapshot['lastHeartbeat']),
		clientTotals: {
			capCallbacks: 30, bytesSent: 1, sendSkipped: 0, chunksRecv: 0,
			chunksScheduled: 0, chunksEnded: 0, chunksCancelled: 0,
		},
		heartbeatCount: 3,
		newEpisodeIds: [],
		samplesSkipped: 0,
		lastTurnLatencyMs: null,
		lastSpeechToModelMs: null,
		lastModelEventAt: o.lastModelEventAt === undefined ? NOW - 20_000 : o.lastModelEventAt,
		inputHealth: 'ok',
		epochStartApproxMs: NOW - 60_000,
		lastMatrixVerdict: null,
		...o,
	} as AudioHealthSnapshot;
}

function baseline(s: AudioHealthSnapshot, over: Partial<MatrixBaseline> = {}): MatrixBaseline {
	return {
		at: NOW - 30_000,
		epoch: s.epoch,
		deliveredFrames: s.deliveredFrames - 500, // ingress advanced by default
		capCallbacks: 0, bytesSent: 0, sendSkipped: 0, chunksRecv: 0,
		chunksScheduled: 0, chunksEnded: 0, chunksCancelled: 0,
		ctxTimeMs: 0, bufferedAmount: 0, serverBufferedAmount: null,
		maxEpisodeId: 0,
		...over,
	};
}

// ── 1. The row-5 shape (the 2026-08-17 incident): all facts true, timestamped.
{
	const s = snapshot({
		speech: { active: false, onsetAt: NOW - 8_000, lastAboveFloorAt: NOW - 4_000 },
		lastModelEventAt: NOW - 40_000,
	});
	const r = evaluateMatrix({
		sessionState: 'ACTIVE', clientConnected: true, snapshot: s,
		prev: baseline(s), lastModelEventAt: NOW - 40_000, now: NOW,
	});
	check('incident shape: verdict stays the honest downgrade', r.verdict, 'insufficient-evidence');
	check('incident shape: facts.factsAvailable', r.facts.factsAvailable, true);
	check('incident shape: facts.speechInWindow', r.facts.speechInWindow, true);
	check('incident shape: facts.speechObservedAt = onset', r.facts.speechObservedAt, NOW - 8_000);
	check('incident shape: facts.ingressAdvanced', r.facts.ingressAdvanced, true);
	check('incident shape: facts.modelSilentFor15s', r.facts.modelSilentFor15s, true);
}

// ── 2. Facts are verdict-independent: healthy-idle still reports true facts
//      where they hold (quiet user, model recently active).
{
	const s = snapshot({
		speech: { active: false, onsetAt: null, lastAboveFloorAt: null },
		lastModelEventAt: NOW - 5_000,
	});
	const r = evaluateMatrix({
		sessionState: 'ACTIVE', clientConnected: true, snapshot: s,
		prev: baseline(s), lastModelEventAt: NOW - 5_000, now: NOW,
	});
	check('quiet user: verdict healthy-idle', r.verdict, 'healthy-idle');
	check('quiet user: speechInWindow false', r.facts.speechInWindow, false);
	check('quiet user: speechObservedAt null iff no speech', r.facts.speechObservedAt, null);
	check('quiet user: modelSilentFor15s false', r.facts.modelSilentFor15s, false);
	check('quiet user: ingressAdvanced true', r.facts.ingressAdvanced, true);
}

// ── 3. Fail closed: no baseline (first tick) ⇒ factsAvailable=false and every
//      fact false, even though speech + silence are real.
{
	const s = snapshot({
		speech: { active: true, onsetAt: NOW - 8_000, lastAboveFloorAt: NOW - 100 },
		lastModelEventAt: NOW - 40_000,
	});
	const r = evaluateMatrix({
		sessionState: 'ACTIVE', clientConnected: true, snapshot: s,
		prev: null, lastModelEventAt: NOW - 40_000, now: NOW,
	});
	check('first tick: factsAvailable false', r.facts.factsAvailable, false);
	check('first tick: all facts fail closed',
		[r.facts.speechInWindow, r.facts.ingressAdvanced, r.facts.modelSilentFor15s],
		[false, false, false]);
	check('first tick: speechObservedAt null', r.facts.speechObservedAt, null);
}

// ── 4. Fail closed: lastModelEventAt === null is NOT silence (fresh session).
{
	const s = snapshot({
		speech: { active: false, onsetAt: NOW - 8_000, lastAboveFloorAt: NOW - 4_000 },
		lastModelEventAt: null,
	});
	const r = evaluateMatrix({
		sessionState: 'ACTIVE', clientConnected: true, snapshot: s,
		prev: baseline(s), lastModelEventAt: null, now: NOW,
	});
	check('null model event: modelSilentFor15s fails closed', r.facts.modelSilentFor15s, false);
}

// ── 5. ingressAdvanced formula: delta(deliveredFrames) > 0, exactly.
{
	const s = snapshot({ lastModelEventAt: NOW - 40_000 });
	const r = evaluateMatrix({
		sessionState: 'ACTIVE', clientConnected: true, snapshot: s,
		prev: baseline(s, { deliveredFrames: s.deliveredFrames }), // zero delta
		lastModelEventAt: NOW - 40_000, now: NOW,
	});
	check('zero ingress delta: ingressAdvanced false', r.facts.ingressAdvanced, false);
}

// ── 6. Epoch boundary ⇒ factsAvailable false (cross-epoch deltas are garbage).
{
	const s = snapshot({ lastModelEventAt: NOW - 40_000 });
	const r = evaluateMatrix({
		sessionState: 'ACTIVE', clientConnected: true, snapshot: s,
		prev: baseline(s, { epoch: 999 }), lastModelEventAt: NOW - 40_000, now: NOW,
	});
	check('epoch boundary: verdict downgrades', r.verdict, 'insufficient-evidence');
	check('epoch boundary: factsAvailable false', r.facts.factsAvailable, false);
}

// ── 7. Reconnect window / detached client: facts stay DATA-level.
// `factsAvailable` reports "a same-epoch baseline exists", not "the session is
// usable" — main's verdict-independence invariant (a structural early return
// carries the same data-level facts) requires that. The structural gate is not
// dropped, it is enforced once, at the consumer: the watchdog's tick branch
// requires `ev.state === 'ACTIVE' && s.clientAttached` before it may fire.
// Folding those into `factsAvailable` would be a second copy of the same gate.
{
	const s = snapshot({});
	const r1 = evaluateMatrix({
		sessionState: 'RECONNECTING', clientConnected: true, snapshot: s,
		prev: baseline(s), lastModelEventAt: NOW - 40_000, now: NOW,
	});
	check('reconnect window: verdict is structural', r1.verdict, 'reconnect-window');
	check('reconnect window: facts stay data-level', r1.facts.factsAvailable, true);
	const r2 = evaluateMatrix({
		sessionState: 'ACTIVE', clientConnected: false, snapshot: s,
		prev: baseline(s), lastModelEventAt: NOW - 40_000, now: NOW,
	});
	check('no client: verdict is healthy-idle', r2.verdict, 'healthy-idle');
	check('no client: facts stay data-level', r2.facts.factsAvailable, true);
}

console.log(failed ? `FAILED: ${failed} check(s)` : 'voice matrix facts: all checks passed');
process.exit(failed ? 1 : 0);
