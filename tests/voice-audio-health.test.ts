import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
	createAudioHealthLedger,
	parseHeartbeat,
	type AudioHealthSnapshot,
	type HealthRow,
} from '../src/voice-audio-health.js';
import { evaluateMatrix } from '../src/voice-health-matrix.js';

/** Int16LE PCM buffer at a constant normalized amplitude. */
function pcm(amplitude: number, samples = 682): Buffer {
	const buf = Buffer.alloc(samples * 2);
	const v = Math.round(amplitude * 32767);
	for (let i = 0; i < samples; i++) buf.writeInt16LE(v, i * 2);
	return buf;
}

/** A minimal valid wire heartbeat (delta counters). */
function wireHb(nonce: string, over: Record<string, unknown> = {}): Record<string, unknown> {
	return {
		t: 'audio_health',
		n: nonce,
		q: 0,
		ea: 60_000,
		c: [47, 64000, 0, 0, 100, 100, 98, 2],
		x: [2000, 3, 120],
		cs: 'r',
		cap: 'o',
		...over,
	};
}

function makeLedger(now: { t: number }, persist?: (row: HealthRow) => boolean) {
	return createAudioHealthLedger({
		sessionId: 'session_test',
		nowFn: () => now.t,
		persist,
		log: () => {},
	});
}

/** Fake session exposing the three wrapped seams. */
function fakeSession() {
	const calls = { audio: [] as unknown[], json: [] as unknown[], out: [] as unknown[] };
	return {
		calls,
		handleAudioFromClient(data: unknown) {
			calls.audio.push(data);
		},
		handleJsonFromClient(msg: unknown) {
			calls.json.push(msg);
		},
		handleAudioOutput(data: unknown) {
			calls.out.push(data);
		},
	};
}

describe('P7 D7.1 parseHeartbeat', () => {
	it('parses a full frame and tolerates unknown fields', () => {
		const hb = parseHeartbeat(
			wireHb('abcd1234', { zz: 'future-field', mu: 1, sc: 2, ba: [100, 900], og: [10, 1500], eo: 3, ep: [[1, 'g', 0, 1200], [2, 's', 5, 9, 300, 172]] }),
			5000,
		);
		assert.ok(hb);
		assert.equal(hb.nonce, 'abcd1234');
		assert.equal(hb.capCallbacks, 47);
		assert.equal(hb.bytesSent, 64000);
		assert.equal(hb.muted, true);
		assert.equal(hb.ctxSuspendCount, 2);
		assert.deepEqual(hb.openGap, { startMs: 10, ageMs: 1500 });
		assert.equal(hb.episodeOverflow, 3);
		assert.deepEqual(hb.episodes, [
			{ id: 1, kind: 'gap', startMs: 0, durationMs: 1200 },
			{ id: 2, kind: 'speech', onsetSeq: 5, offsetSeq: 9, maxRmsPm: 300, aboveFloorMs: 172 },
		]);
		assert.equal(hb.receivedAt, 5000);
	});

	it('rejects non-heartbeat shapes', () => {
		assert.equal(parseHeartbeat(null, 0), null);
		assert.equal(parseHeartbeat({ t: 'other' }, 0), null);
		assert.equal(parseHeartbeat({ t: 'audio_health' }, 0), null); // no nonce
		assert.equal(parseHeartbeat('audio_health', 0), null);
	});

	it('tolerates missing optionals (absent c/x/ba/og/ep)', () => {
		const hb = parseHeartbeat({ t: 'audio_health', n: 'x1', q: 4 }, 1);
		assert.ok(hb);
		assert.equal(hb.capCallbacks, 0);
		assert.equal(hb.openGap, null);
		assert.deepEqual(hb.episodes, []);
	});
});

describe('P7 D7.1 engine ledger — wraps', () => {
	it('audio wrap is transparent (same Buffer through) and counts delivered frames', () => {
		const now = { t: 10_000 };
		const led = makeLedger(now);
		const s = fakeSession();
		led.wrapSession(s);
		const b = pcm(0.3);
		s.handleAudioFromClient(b);
		assert.equal(s.calls.audio.length, 1);
		assert.equal(s.calls.audio[0], b, 'exact same Buffer delivered');
		const snap = led.getSnapshot(true);
		assert.equal(snap.deliveredFrames, 1);
		assert.equal(snap.deliveredBytes, b.length);
		assert.equal(snap.coverage, 'session-only');
	});

	it('audio_health frames are intercepted (never routed into bodhi); other JSON passes through', () => {
		const now = { t: 10_000 };
		const led = makeLedger(now);
		const s = fakeSession();
		led.wrapSession(s);
		s.handleJsonFromClient(wireHb('n0n0n0n0'));
		s.handleJsonFromClient({ type: 'text_input', text: 'hi' });
		assert.equal(s.calls.json.length, 1, 'heartbeat swallowed');
		assert.deepEqual(s.calls.json[0], { type: 'text_input', text: 'hi' });
		assert.equal(led.getSnapshot(true).heartbeatCount, 1);
	});

	it('a malformed heartbeat never breaks the message path', () => {
		const led = makeLedger({ t: 0 });
		const s = fakeSession();
		led.wrapSession(s);
		assert.doesNotThrow(() => s.handleJsonFromClient({ t: 'audio_health', n: 42, c: 'bogus' }));
	});

	it('egress wrap counts frames/bytes and passes through; wrap is idempotent', () => {
		const now = { t: 10_000 };
		const led = makeLedger(now);
		const s = fakeSession();
		led.wrapSession(s);
		led.wrapSession(s); // second wrap must be a no-op
		const b64 = Buffer.from('abcdef').toString('base64');
		s.handleAudioOutput(b64);
		assert.equal(s.calls.out.length, 1, 'idempotent wrap: exactly one delivery');
		const snap = led.getSnapshot(true);
		assert.equal(snap.egressFrames, 1);
		assert.equal(snap.egressBytes, 6);
	});
});

describe('P7 D7.1 engine ledger — epoch minting (Tranche A nonce scheme)', () => {
	it('mints AT CONNECT; the nonce binds on first heartbeat; a connection dying pre-heartbeat still has a real epoch', () => {
		const now = { t: 50_000 };
		const led = makeLedger(now);
		led.onClientConnected();
		const e1 = led.getSnapshot(true).epoch;
		assert.ok(e1 && e1 >= 50_000, 'epoch minted at connect, before any heartbeat');
		assert.equal(led.getSnapshot(true).nonce, null, 'nonce pending until first heartbeat');
		led.ingestHeartbeat(wireHb('nonceAAA'));
		assert.equal(led.getSnapshot(true).epoch, e1, 'first heartbeat BINDS, does not re-mint');
		assert.equal(led.getSnapshot(true).nonce, 'nonceAAA');
		led.ingestHeartbeat(wireHb('nonceAAA', { q: 1 }));
		assert.equal(led.getSnapshot(true).clientTotals.capCallbacks, 94, 'deltas accumulate');
		// Reconnect: connect mints a strictly newer epoch and resets baselines.
		now.t += 10;
		led.onClientConnected();
		led.ingestHeartbeat(wireHb('nonceBBB'));
		const snap = led.getSnapshot(true);
		assert.ok(snap.epoch && snap.epoch > e1!, 'new connection = strictly newer epoch');
		assert.equal(snap.nonce, 'nonceBBB');
		assert.equal(snap.clientTotals.capCallbacks, 47, 'totals reset at the epoch boundary');
	});

	it('a mid-connection nonce change (no connect event) defensively re-mints', () => {
		const now = { t: 50_000 };
		const led = makeLedger(now);
		led.onClientConnected();
		led.ingestHeartbeat(wireHb('nonceAAA'));
		const e1 = led.getSnapshot(true).epoch!;
		now.t += 10;
		led.ingestHeartbeat(wireHb('nonceZZZ'));
		assert.ok(led.getSnapshot(true).epoch! > e1, 'unexpected nonce never reuses the old epoch');
	});

	it('onClientConnected resets per-epoch counters, latency samples, and skip counters', () => {
		const now = { t: 50_000 };
		const rows: HealthRow[] = [];
		let accept = false;
		const led = makeLedger(now, () => accept);
		const s = fakeSession();
		led.wrapSession(s);
		s.handleAudioFromClient(pcm(0.1));
		led.ingestHeartbeat(wireHb('nonceAAA'));
		led.noteTurnLatency(1234);
		led.persistTick('timer', true); // rejected → samplesSkipped 1
		assert.equal(led.getSnapshot(true).samplesSkipped, 1);
		led.onClientConnected();
		accept = true;
		const snap = led.getSnapshot(true);
		assert.ok(snap.epoch, 'fresh epoch minted');
		assert.equal(snap.deliveredFrames, 0);
		assert.equal(snap.heartbeatCount, 0);
		assert.equal(snap.samplesSkipped, 0, 'skip counter is per-epoch');
		assert.equal(snap.lastTurnLatencyMs, null, 'latency sample is per-epoch');
		assert.equal(rows.length, 0);
	});
});

describe('P7 D7.1 engine ledger — ingress speech tracker (canonical evidence)', () => {
	it('latches onset on loud PCM and decays lazily after the hangover', () => {
		const now = { t: 10_000 };
		const led = makeLedger(now);
		const s = fakeSession();
		led.wrapSession(s);
		s.handleAudioFromClient(pcm(0.3));
		let ev = led.getSpeechEvidence();
		assert.equal(ev.active, true);
		assert.equal(ev.onsetAt, 10_000);
		now.t += 300;
		s.handleAudioFromClient(pcm(0.25));
		now.t += 700; // no frames at all (stall) — lazy decay must end the evidence
		ev = led.getSpeechEvidence();
		assert.equal(ev.active, false, 'hangover elapsed with no above-floor frame');
		assert.equal(ev.lastAboveFloorAt, 10_300);
	});

	it('retains the utterance onset after the hangover (30s matrix window needs the FIRST sample)', () => {
		const now = { t: 10_000 };
		const led = makeLedger(now);
		const s = fakeSession();
		led.wrapSession(s);
		s.handleAudioFromClient(pcm(0.3));
		now.t += 300;
		s.handleAudioFromClient(pcm(0.25));
		now.t += 20_000; // utterance long over; live onset erased by decay
		const ev = led.getSpeechEvidence();
		assert.equal(ev.active, false);
		assert.equal(ev.onsetAt, null, 'live onset is hangover-scoped');
		assert.equal(ev.lastOnsetAt, 10_000, 'retained onset survives for the evidence window');
	});

	it('quiet PCM is not speech', () => {
		const led = makeLedger({ t: 10_000 });
		const s = fakeSession();
		led.wrapSession(s);
		s.handleAudioFromClient(pcm(0.005));
		assert.equal(led.getSpeechEvidence().active, false);
	});

	it('speech after a silent stall is a NEW onset (eager decay on the frame path)', () => {
		const now = { t: 10_000 };
		const led = makeLedger(now);
		const s = fakeSession();
		led.wrapSession(s);
		s.handleAudioFromClient(pcm(0.3)); // onset #1 at 10000
		now.t = 10_700; // hangover elapsed with NO frames (stall)
		s.handleAudioFromClient(pcm(0.3)); // must re-latch, not extend
		const ev = led.getSpeechEvidence();
		assert.equal(ev.active, true);
		assert.equal(ev.onsetAt, 10_700, 'stale onset would corrupt speech→model latency');
	});

	it('speech onset → first model audio yields the latency sample (server clock)', () => {
		const now = { t: 10_000 };
		const led = makeLedger(now);
		const s = fakeSession();
		led.wrapSession(s);
		s.handleAudioFromClient(pcm(0.3)); // onset armed at 10000
		now.t = 11_840;
		s.handleAudioOutput(Buffer.from('audio').toString('base64'));
		assert.equal(led.getSnapshot(true).lastSpeechToModelMs, 1840);
		// consumed: a second egress frame does not overwrite the sample
		now.t = 12_000;
		s.handleAudioOutput(Buffer.from('audio').toString('base64'));
		assert.equal(led.getSnapshot(true).lastSpeechToModelMs, 1840);
	});

	it('ingress RMS work is bounded: a jumbo frame scans at most 8 KiB', () => {
		const led = makeLedger({ t: 10_000 });
		const s = fakeSession();
		led.wrapSession(s);
		const jumbo = Buffer.alloc(4 * 1024 * 1024); // 4 MiB of silence
		const t0 = process.hrtime.bigint();
		s.handleAudioFromClient(jumbo);
		const us = Number(process.hrtime.bigint() - t0) / 1000;
		assert.ok(us < 5000, `jumbo frame took ${us.toFixed(0)}µs — scan not bounded`);
		assert.equal(led.getSnapshot(true).deliveredBytes, jumbo.length, 'bytes still fully accounted');
	});
});

describe('P7 D7.1 engine ledger — inputHealth + anomaly latching', () => {
	it('inputHealth: no-client / unknown / ok / stalled(og) / stalled(ingress-stale)', () => {
		const now = { t: 10_000 };
		const led = makeLedger(now);
		const s = fakeSession();
		led.wrapSession(s);
		assert.equal(led.getInputHealth(false), 'no-client');
		assert.equal(led.getInputHealth(true), 'unknown', 'no evidence yet');
		led.onClientConnected();
		s.handleAudioFromClient(pcm(0.1));
		assert.equal(led.getInputHealth(true), 'ok');
		led.ingestHeartbeat(wireHb('nnnnnnnn', { og: [0, 1500] }));
		assert.equal(led.getInputHealth(true), 'stalled', 'client-reported open gap');
		led.ingestHeartbeat(wireHb('nnnnnnnn', { q: 1 })); // gap closed
		assert.equal(led.getInputHealth(true), 'ok');
		now.t += 6000; // ingress silent past the stall bound while unmuted
		assert.equal(led.getInputHealth(true), 'stalled');
	});

	it('heartbeats crossing while NO PCM ever reached the session is a stall, not health', () => {
		const now = { t: 10_000 };
		const led = makeLedger(now);
		led.onClientConnected();
		led.ingestHeartbeat(wireHb('nnnnnnnn'));
		assert.equal(led.getInputHealth(true), 'stalled');
	});

	it('a muted client is never stalled; a degraded capture surfaces even muted', () => {
		const now = { t: 10_000 };
		const led = makeLedger(now);
		const s = fakeSession();
		led.wrapSession(s);
		s.handleAudioFromClient(pcm(0.1));
		now.t += 6000; // would be an ingress stall if unmuted
		led.ingestHeartbeat(wireHb('nnnnnnnn', { mu: 1 }));
		assert.equal(led.getInputHealth(true), 'ok', 'mute is a user choice');
		led.ingestHeartbeat(wireHb('nnnnnnnn', { q: 1, mu: 1, cap: 'd' }));
		assert.equal(led.getInputHealth(true), 'degraded', 'exhausted recovery surfaces regardless');
	});

	it('suspended/closed context reports stalled; STALE heartbeat evidence stops counting', () => {
		const now = { t: 10_000 };
		const led = makeLedger(now);
		const s = fakeSession();
		led.wrapSession(s);
		s.handleAudioFromClient(pcm(0.1));
		led.ingestHeartbeat(wireHb('nnnnnnnn', { cs: 's' }));
		assert.equal(led.getInputHealth(true), 'stalled', 'suspended ctx = no input');
		// The same heartbeat 20 s later is stale — a detached client's
		// retained evidence must not report stalled forever…
		now.t += 20_000;
		s.handleAudioFromClient(pcm(0.1)); // …and live PCM proves input works
		assert.equal(led.getInputHealth(true), 'ok');
	});

	it('anomalies PEEK until clearTickLatches; muted/stale gates apply', () => {
		const now = { t: 10_000 };
		const led = makeLedger(now);
		const s = fakeSession();
		led.wrapSession(s);
		s.handleAudioFromClient(pcm(0.1));
		led.ingestHeartbeat(wireHb('nnnnnnnn', { ep: [[1, 'g', 0, 1200]], og: [0, 1500] }));
		const a1 = led.anomalies(true);
		assert.equal(a1.anomalous, true);
		assert.ok(a1.reasons.includes('capStalled'));
		assert.ok(a1.reasons.some((r) => r.startsWith('episodes:')));
		const a1again = led.anomalies(true);
		assert.ok(a1again.reasons.some((r) => r.startsWith('episodes:')), 'peek does not clear');
		led.clearTickLatches();
		// same episode re-sent (idempotent window) → no NEW episode anomaly
		led.ingestHeartbeat(wireHb('nnnnnnnn', { q: 1, ep: [[1, 'g', 0, 1200]], og: [0, 3500] }));
		const a2 = led.anomalies(true);
		assert.ok(!a2.reasons.some((r) => r.startsWith('episodes:')), 'dedup by episode id');
		led.clearTickLatches();
		now.t += 6000;
		led.ingestHeartbeat(wireHb('nnnnnnnn', { q: 2, mu: 1, og: [0, 9000] }));
		const a3 = led.anomalies(true);
		assert.ok(!a3.reasons.includes('ingress-stalled'), 'muted client is not an ingress stall');
		assert.ok(!a3.reasons.includes('capStalled'), 'muted open gap is not a stall');
		assert.ok(!led.anomalies(false).reasons.includes('capStalled'), 'detached gap is not live');
	});

	it('sendSkipped delta in any heartbeat is an anomaly', () => {
		const led = makeLedger({ t: 10_000 });
		led.ingestHeartbeat(wireHb('nnnnnnnn', { c: [47, 64000, 5, 0, 0, 0, 0, 0] }));
		assert.ok(led.anomalies(true).reasons.includes('sendSkipped'));
	});

	it('unknown episode kinds are dropped, never misclassified as speech', () => {
		const led = makeLedger({ t: 10_000 });
		led.ingestHeartbeat(wireHb('nnnnnnnn', { ep: [[1, 'z', 0, 1200]] }));
		assert.equal(led.getSnapshot(true).lastHeartbeat?.episodes.length, 0);
	});

	it('the ep array ingest is capped at 8 entries', () => {
		const led = makeLedger({ t: 10_000 });
		const ep = Array.from({ length: 100 }, (_, i) => [i + 1, 'g', 0, 100]);
		led.ingestHeartbeat(wireHb('nnnnnnnn', { ep }));
		assert.equal(led.getSnapshot(true).lastHeartbeat?.episodes.length, 8);
	});
});

describe('P7 D7.1 engine ledger — persistence tick + [Health] segments', () => {
	it('persistTick writes a bounded row; a busy mailbox skips the sample visibly', () => {
		const now = { t: 10_000 };
		const rows: HealthRow[] = [];
		let accept = true;
		const led = makeLedger(now, (row) => {
			if (accept) rows.push(row);
			return accept;
		});
		led.ingestHeartbeat(wireHb('nnnnnnnn'));
		led.persistTick('timer', true);
		assert.equal(rows.length, 1);
		assert.equal(rows[0].sessionId, 'session_test');
		assert.equal(rows[0].reason, 'timer');
		assert.equal(rows[0].nonce, 'nnnnnnnn');
		const payload = JSON.parse(rows[0].payload);
		assert.equal(payload.coverage, 'session-only');
		accept = false;
		led.persistTick('anomaly', true);
		assert.equal(led.getSnapshot(true).samplesSkipped, 1, 'busy mailbox → skipped, never queued');
		assert.ok(led.anomalies(true).reasons.includes('persistSkipped'));
	});

	it('an oversized snapshot persists as reduced-but-VALID JSON, never a sliced document', () => {
		const now = { t: 10_000 };
		const rows: HealthRow[] = [];
		const led = makeLedger(now, (row) => {
			rows.push(row);
			return true;
		});
		led.onClientConnected();
		// Inflate the snapshot past the payload budget: hundreds of un-cleared
		// new episode ids (a tick that never cleared its latches).
		for (let k = 0; k < 80; k++) {
			// 8-digit ids keep every entry wide so the accumulated ids alone
			// push the snapshot past the payload budget.
			const ep = Array.from({ length: 8 }, (_, i) => [10_000_000 + k * 8 + i, 'g', 9_999_999, 9_999_999]);
			led.ingestHeartbeat(wireHb('nnnnnnnn', { q: k, ep }));
		}
		led.persistTick('timer', true);
		assert.equal(rows.length, 1);
		const parsed = JSON.parse(rows[0].payload); // must not throw — VALID JSON
		assert.ok(Buffer.byteLength(rows[0].payload) <= 4096);
		assert.equal(parsed.truncated, true, 'over-budget snapshot takes the reduced form');
		assert.equal(parsed.coverage, 'session-only');
		assert.ok(parsed.epoch, 'core evidence survives truncation');
	});

	it('healthSegments renders the upgraded line fields', () => {
		const now = { t: 10_000 };
		const led = makeLedger(now);
		const s = fakeSession();
		led.wrapSession(s);
		led.onClientConnected();
		s.handleAudioFromClient(pcm(0.3));
		led.ingestHeartbeat(wireHb('nnnnnnnn'));
		now.t += 2000;
		led.ingestHeartbeat(wireHb('nnnnnnnn', { q: 1 }));
		led.noteTurnLatency(1840);
		const line = led.healthSegments(true);
		assert.match(line, /audioIn=\{fps:[\d.]+,lastAgo:[\d.]+s,coverage:session-only\}/);
		assert.match(line, /up=n\/a/);
		assert.match(line, /out=\{fps:/);
		assert.match(line, /clientHealth=\{age:[\d.]+s,capCb\/s:23\.5,rms:/);
		assert.match(line, /latency=1840ms/);
		assert.match(line, /inputHealth=ok/);
	});
});

describe('P7 round-3 ledger fixes', () => {
	it('epoch placement uses the client-stamped epoch age (receivedAt − ea), refreshed per beat', () => {
		const now = { t: 100_000 };
		const led = makeLedger(now);
		led.onClientConnected();
		led.ingestHeartbeat(wireHb('nnnnnnnn', { ea: 7_500 }));
		assert.equal(led.getSnapshot(true).epochStartApproxMs, 100_000 - 7_500);
		now.t = 102_000;
		led.ingestHeartbeat(wireHb('nnnnnnnn', { q: 1, ea: 9_500 }));
		assert.equal(led.getSnapshot(true).epochStartApproxMs, 102_000 - 9_500);
	});

	it('a frame without ea falls back to the coarse first-beat guess', () => {
		const now = { t: 100_000 };
		const led = makeLedger(now);
		led.onClientConnected();
		const frame = wireHb('nnnnnnnn');
		delete (frame as Record<string, unknown>).ea;
		led.ingestHeartbeat(frame);
		assert.equal(led.getSnapshot(true).epochStartApproxMs, 100_000 - 500);
	});

	it('an unexpected nonce is a FULL epoch boundary: server counters and latency reset too', () => {
		const now = { t: 100_000 };
		const led = makeLedger(now);
		const s = fakeSession();
		led.wrapSession(s);
		led.onClientConnected();
		led.ingestHeartbeat(wireHb('nonceAAA'));
		s.handleAudioFromClient(pcm(0.1));
		led.noteTurnLatency(1234);
		now.t += 10;
		led.ingestHeartbeat(wireHb('nonceZZZ'));
		const snap = led.getSnapshot(true);
		assert.equal(snap.deliveredFrames, 0, 'server-side counters reset at the boundary');
		assert.equal(snap.lastTurnLatencyMs, null);
		assert.equal(snap.nonce, 'nonceZZZ');
	});

	it('noteModelEvent records model activity and consumes the speech→model latency sample', () => {
		const now = { t: 10_000 };
		const led = makeLedger(now);
		const s = fakeSession();
		led.wrapSession(s);
		s.handleAudioFromClient(pcm(0.3)); // speech onset armed at 10000
		now.t = 10_900;
		led.noteModelEvent(); // e.g. turn.start — text/tool-first turn
		const snap = led.getSnapshot(true);
		assert.equal(snap.lastModelEventAt, 10_900);
		assert.equal(snap.lastSpeechToModelMs, 900, 'first EVENT, not first audio');
		now.t = 11_500;
		s.handleAudioOutput(Buffer.from('audio').toString('base64'));
		assert.equal(led.getSnapshot(true).lastSpeechToModelMs, 900, 'already consumed');
		assert.equal(led.getSnapshot(true).lastModelEventAt, 11_500, 'audio still advances the event clock');
	});

	it('episode anomalies require an attached client (a detached window re-send is not live)', () => {
		const now = { t: 10_000 };
		const led = makeLedger(now);
		led.ingestHeartbeat(wireHb('nnnnnnnn', { ep: [[1, 'g', 0, 1200]] }));
		assert.ok(!led.anomalies(false).reasons.some((r) => r.startsWith('episodes:')));
		assert.ok(led.anomalies(true).reasons.some((r) => r.startsWith('episodes:')));
	});
});

describe('P7 Tranche B — lineage, context occupancy, up= segment', () => {
	function upstreamSample(over: Record<string, unknown> = {}) {
		const slot = {
			attempted: 0,
			queued: 0,
			skippedNoSession: 0,
			threw: 0,
			attemptedRawBytes: 0,
			queuedRawBytes: 0,
			attemptedWireBytesEstimate: 0,
			queuedWireBytesEstimate: 0,
			lastAttemptedAt: null,
			lastQueuedAt: null,
			lastSkippedAt: null,
			lastThrewAt: null,
		};
		return {
			upstream: {
				audio: { ...slot, queued: 235, queuedWireBytesEstimate: 400_000, ...over },
				video: { ...slot, unsupportedMime: 0 },
				text: { ...slot, skippedEmpty: 0 },
			},
			transportGeneration: 1,
			echoSuppressed: 0,
		};
	}

	it('lineage: minted on a handle-less setup-ok; resumed keeps it; suspected-sever is terminal', () => {
		const ledger = createAudioHealthLedger({ sessionId: 's' });
		ledger.noteLifecycleEvent({ kind: 'attempt', connectAttemptId: 'att_1', handleSupplied: false });
		ledger.noteLifecycleEvent({ kind: 'setup-ok', connectAttemptId: 'att_1', transportGeneration: 1 });
		let s = ledger.getSnapshot(true);
		assert.equal(s.logicalSessionId, 1);
		assert.equal(s.lineageState, 'fresh');

		ledger.noteLifecycleEvent({ kind: 'attempt', connectAttemptId: 'att_2', handleSupplied: true });
		ledger.noteLifecycleEvent({ kind: 'setup-ok', connectAttemptId: 'att_2', transportGeneration: 2 });
		s = ledger.getSnapshot(true);
		assert.equal(s.logicalSessionId, 1, 'a resumed setup keeps the lineage');
		assert.equal(s.lineageState, 'resumed');

		// Handle-accepted 1008 close: suspected-sever, and TERMINAL — a later
		// resumed setup-ok must not silently clear the suspicion.
		ledger.noteLifecycleEvent({
			kind: 'generation-close',
			connectAttemptId: 'att_2',
			transportGeneration: 2,
			code: 1008,
			reason: 'Requested entity was not found.',
		});
		ledger.noteLifecycleEvent({ kind: 'attempt', connectAttemptId: 'att_3', handleSupplied: true });
		ledger.noteLifecycleEvent({ kind: 'setup-ok', connectAttemptId: 'att_3', transportGeneration: 3 });
		s = ledger.getSnapshot(true);
		assert.equal(s.lineageState, 'suspected-sever');

		// Only a FRESH (handle-less) lineage exits the state, by minting a new id.
		ledger.noteLifecycleEvent({ kind: 'attempt', connectAttemptId: 'att_4', handleSupplied: false });
		ledger.noteLifecycleEvent({ kind: 'setup-ok', connectAttemptId: 'att_4', transportGeneration: 4 });
		s = ledger.getSnapshot(true);
		assert.equal(s.logicalSessionId, 2);
		assert.equal(s.lineageState, 'fresh');
	});

	it('a setup failure on a supplied handle is suspected-sever', () => {
		const ledger = createAudioHealthLedger({ sessionId: 's' });
		ledger.noteLifecycleEvent({ kind: 'attempt', connectAttemptId: 'att_1', handleSupplied: true });
		ledger.noteLifecycleEvent({ kind: 'setup-failed', connectAttemptId: 'att_1', reason: 'timeout' });
		assert.equal(ledger.getSnapshot(true).lineageState, 'suspected-sever');
	});

	it('context occupancy latches per lineage and resets on a fresh one', () => {
		const t = 1_000_000;
		const ledger = createAudioHealthLedger({ sessionId: 's', nowFn: () => t });
		ledger.noteUsageMetadata(41_200);
		let s = ledger.getSnapshot(true);
		assert.equal(s.contextTokens, 41_200);
		assert.equal(s.contextTokensAt, 1_000_000);
		ledger.noteUsageMetadata(Number.NaN); // garbage never overwrites a reading
		assert.equal(ledger.getSnapshot(true).contextTokens, 41_200);

		ledger.noteLifecycleEvent({ kind: 'attempt', connectAttemptId: 'a', handleSupplied: false });
		ledger.noteLifecycleEvent({ kind: 'setup-ok', connectAttemptId: 'a', transportGeneration: 1 });
		s = ledger.getSnapshot(true);
		assert.equal(s.contextTokens, null, 'a fresh lineage starts with unknown occupancy');
	});

	it('healthSegments renders up= from diagnostics deltas, and ctx= when usage was seen', () => {
		let t = 1_000_000;
		const diag = upstreamSample();
		const ledger = createAudioHealthLedger({
			sessionId: 's',
			nowFn: () => t,
			getSessionDiagnostics: () => diag,
		});
		ledger.noteUsageMetadata(9_000);
		ledger.healthSegments(true); // first tick establishes the window
		t += 30_000;
		diag.upstream.audio.queued += 705; // 23.5/s over 30 s
		diag.upstream.audio.queuedWireBytesEstimate += 705 * 1364;
		const line = ledger.healthSegments(true);
		assert.match(line, /up=\{aQ\/s:23\.5,/);
		assert.match(line, /ctx=\{tok:9000,age:30\.0s\}/);
	});

	it('a transport-generation change renders that tick from zero, never a negative rate', () => {
		let t = 1_000_000;
		const diag = upstreamSample();
		const ledger = createAudioHealthLedger({
			sessionId: 's',
			nowFn: () => t,
			getSessionDiagnostics: () => diag,
		});
		ledger.healthSegments(true);
		t += 30_000;
		// Reconnect: counters reset, generation bumps, small new count.
		diag.transportGeneration = 2;
		diag.upstream.audio.queued = 10;
		diag.upstream.audio.queuedWireBytesEstimate = 14_000;
		const line = ledger.healthSegments(true);
		assert.match(line, /up=\{aQ\/s:0\.3,/, 'renders the new generation from zero');
	});

	it('up= stays n/a when diagnostics are unobserved or throw', () => {
		const none = createAudioHealthLedger({ sessionId: 's' });
		assert.match(none.healthSegments(true), /up=n\/a/);
		const throwing = createAudioHealthLedger({
			sessionId: 's',
			getSessionDiagnostics: () => {
				throw new Error('boom');
			},
		});
		assert.match(throwing.healthSegments(true), /up=n\/a/);
	});

	it('the persisted row carries every evaluator input (replayable — design §1.6)', () => {
		let persisted: HealthRow | null = null;
		const diag = upstreamSample({ attempted: 300, queued: 235, lastQueuedAt: 999_500 });
		const ledger = createAudioHealthLedger({
			sessionId: 's',
			nowFn: () => 1_000_000,
			getSessionDiagnostics: () => diag,
			persist: (row) => {
				persisted = row;
				return true;
			},
		});
		ledger.noteLifecycleEvent({ kind: 'attempt', connectAttemptId: 'a', handleSupplied: false });
		ledger.noteLifecycleEvent({ kind: 'setup-ok', connectAttemptId: 'a', transportGeneration: 1 });
		ledger.noteUsageMetadata(4_096);
		ledger.persistTick('timer', true);
		assert.ok(persisted, 'row persisted');
		const full = JSON.parse((persisted as HealthRow).payload);
		assert.equal(full.logicalSessionId, 1);
		assert.equal(full.transportGeneration, 1);
		assert.equal(full.contextTokens, 4_096);
		assert.equal(full.upstream.audio.attempted, 300);
		assert.equal(full.upstream.audio.lastQueuedAt, 999_500);
	});

	it('a missed diagnostics sample never fabricates an up= rate (baselining tick)', () => {
		// codex round-5 #5 repro: a missed sample, then 900 cumulative queues —
		// rendering 30.0/s from one 30 s dt would be a fabricated window.
		let t = 1_000_000;
		let observable = true;
		const diag = upstreamSample();
		const ledger = createAudioHealthLedger({
			sessionId: 's',
			nowFn: () => t,
			getSessionDiagnostics: () => (observable ? diag : null),
		});
		assert.match(ledger.healthSegments(true), /up=n\/a\(baselining\)/, 'first observed tick');
		t += 30_000;
		assert.match(ledger.healthSegments(true), /up=\{aQ\/s:0\.0,/, 'window established');
		t += 30_000;
		observable = false; // missed sample invalidates the baseline
		assert.match(ledger.healthSegments(true), /up=n\/a/);
		t += 30_000;
		observable = true;
		diag.upstream.audio.queued += 900;
		assert.match(
			ledger.healthSegments(true),
			/up=n\/a\(baselining\)/,
			'no adjacent baseline — never 30.0/s',
		);
		t += 30_000;
		diag.upstream.audio.queued += 30;
		assert.match(ledger.healthSegments(true), /up=\{aQ\/s:1\.0,/, 'rates resume next tick');
	});

	it('lineage commits and re-labels emit §1.1 reconciliation records (reason: lineage)', () => {
		const rows: HealthRow[] = [];
		const ledger = createAudioHealthLedger({
			sessionId: 's',
			nowFn: () => 1_000_000,
			persist: (row) => {
				rows.push(row);
				return true;
			},
		});
		ledger.noteLifecycleEvent({ kind: 'attempt', connectAttemptId: 'att_1', handleSupplied: false });
		ledger.noteLifecycleEvent({ kind: 'setup-ok', connectAttemptId: 'att_1', transportGeneration: 1 });
		ledger.noteLifecycleEvent({ kind: 'attempt', connectAttemptId: 'att_2', handleSupplied: true });
		ledger.noteLifecycleEvent({ kind: 'setup-failed', connectAttemptId: 'att_2', reason: 'timeout' });
		const recs = rows.filter((r) => r.reason === 'lineage').map((r) => JSON.parse(r.payload));
		assert.equal(recs.length, 2, 'one record per commit/re-label');
		assert.deepEqual(recs[0], {
			connectAttemptId: 'att_1',
			transportGeneration: 1,
			logicalSessionId: 1,
			lineageState: 'fresh',
		});
		assert.deepEqual(recs[1], {
			connectAttemptId: 'att_2',
			transportGeneration: null,
			logicalSessionId: 1,
			lineageState: 'suspected-sever',
		});
		// A handle-less setup failure never commits — no record (lineage stays provisional).
		ledger.noteLifecycleEvent({ kind: 'attempt', connectAttemptId: 'att_3', handleSupplied: false });
		ledger.noteLifecycleEvent({ kind: 'setup-failed', connectAttemptId: 'att_3' });
		assert.equal(rows.filter((r) => r.reason === 'lineage').length, 2);
	});

	it('the per-modality context breakdown latches and resets with the lineage (§1.4)', () => {
		const ledger = createAudioHealthLedger({ sessionId: 's', nowFn: () => 1_000_000 });
		ledger.noteUsageMetadata(1000, [
			{ modality: 'VIDEO', tokenCount: 600 },
			{ modality: 'AUDIO', tokenCount: 300 },
			{ modality: 'AUDIO', tokenCount: 50 }, // duplicate modalities accumulate
			{ tokenCount: 7 }, // malformed entries are ignored
		]);
		let s = ledger.getSnapshot(true);
		assert.deepEqual(s.contextTokensDetails, { VIDEO: 600, AUDIO: 350 });
		ledger.noteLifecycleEvent({ kind: 'attempt', connectAttemptId: 'a', handleSupplied: false });
		ledger.noteLifecycleEvent({ kind: 'setup-ok', connectAttemptId: 'a', transportGeneration: 1 });
		s = ledger.getSnapshot(true);
		assert.equal(s.contextTokensDetails, null, 'breakdown belongs to the lineage, like the scalar');
	});

	it('a REDUCED row still replays: snapshot-shaped evaluator inputs + recorded decision (§1.6)', () => {
		const now = { t: 10_000 };
		const rows: HealthRow[] = [];
		const led = makeLedger(now, (row) => {
			rows.push(row);
			return true;
		});
		led.onClientConnected();
		led.noteMatrixVerdict('healthy-idle', { attemptedAudioAdvanced: true }, ['r1']);
		for (let k = 0; k < 80; k++) {
			const ep = Array.from({ length: 8 }, (_, i) => [10_000_000 + k * 8 + i, 'g', 9_999_999, 9_999_999]);
			led.ingestHeartbeat(wireHb('nnnnnnnn', { q: k, ep }));
		}
		led.persistTick('timer', true);
		const full = led.getSnapshot(true);
		const parsed = JSON.parse(rows[0].payload) as AudioHealthSnapshot & { truncated?: boolean };
		assert.equal(parsed.truncated, true, 'this fixture exercises the REDUCED form');
		// The recorded decision rides (§1.6 option 2).
		assert.equal(parsed.lastMatrixVerdict, 'healthy-idle');
		assert.deepEqual(parsed.lastMatrixFacts, { attemptedAudioAdvanced: true });
		assert.deepEqual(parsed.lastMatrixReasons, ['r1']);
		// Replayable (§1.6 option 1): the evaluator sees full and reduced rows
		// IDENTICALLY — same baseline (every diff source), same verdict, same facts.
		const evalOn = (snapshot: AudioHealthSnapshot, prev: null | ReturnType<typeof evaluateMatrix>['baseline'], at: number) =>
			evaluateMatrix({ sessionState: 'ACTIVE', clientConnected: true, snapshot, prev, now: at });
		const rFull = evalOn(full, null, now.t);
		const rRed = evalOn(parsed, null, now.t);
		assert.deepEqual(rRed.baseline, rFull.baseline, 'every evaluator diff source survives reduction');
		const r2Full = evalOn(full, rFull.baseline, now.t + 30_000);
		const r2Red = evalOn(parsed, rFull.baseline, now.t + 30_000);
		assert.equal(r2Red.verdict, r2Full.verdict);
		assert.deepEqual(r2Red.facts, r2Full.facts);
	});
});
