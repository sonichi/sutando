import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
	createAudioHealthLedger,
	parseHeartbeat,
	type HealthRow,
} from '../src/voice-audio-health.js';

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
	it('mints on first sight, stays stable per nonce, advances per new nonce, resets baselines', () => {
		const now = { t: 50_000 };
		const led = makeLedger(now);
		led.ingestHeartbeat(wireHb('nonceAAA'));
		const e1 = led.getSnapshot(true).epoch;
		assert.ok(e1 && e1 >= 50_000, 'engine-minted epoch');
		led.ingestHeartbeat(wireHb('nonceAAA', { q: 1 }));
		assert.equal(led.getSnapshot(true).epoch, e1, 'stable for the same connection');
		assert.equal(led.getSnapshot(true).clientTotals.capCallbacks, 94, 'deltas accumulate');
		now.t += 10;
		led.ingestHeartbeat(wireHb('nonceBBB'));
		const snap = led.getSnapshot(true);
		assert.ok(snap.epoch && snap.epoch > e1!, 'new connection = strictly newer epoch');
		assert.equal(snap.nonce, 'nonceBBB');
		assert.equal(snap.clientTotals.capCallbacks, 47, 'totals reset at the epoch boundary');
	});

	it('onClientConnected clears the pending epoch and per-epoch counters', () => {
		const now = { t: 50_000 };
		const led = makeLedger(now);
		const s = fakeSession();
		led.wrapSession(s);
		s.handleAudioFromClient(pcm(0.1));
		led.ingestHeartbeat(wireHb('nonceAAA'));
		led.onClientConnected();
		const snap = led.getSnapshot(true);
		assert.equal(snap.epoch, null);
		assert.equal(snap.deliveredFrames, 0);
		assert.equal(snap.heartbeatCount, 0);
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

	it('quiet PCM is not speech', () => {
		const led = makeLedger({ t: 10_000 });
		const s = fakeSession();
		led.wrapSession(s);
		s.handleAudioFromClient(pcm(0.005));
		assert.equal(led.getSpeechEvidence().active, false);
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
		s.handleAudioFromClient(pcm(0.1));
		assert.equal(led.getInputHealth(true), 'ok');
		led.ingestHeartbeat(wireHb('nnnnnnnn', { og: [0, 1500] }));
		assert.equal(led.getInputHealth(true), 'stalled', 'client-reported open gap');
		led.ingestHeartbeat(wireHb('nnnnnnnn', { q: 1 })); // gap closed
		assert.equal(led.getInputHealth(true), 'ok');
		now.t += 6000; // ingress silent past the stall bound while unmuted
		assert.equal(led.getInputHealth(true), 'stalled');
	});

	it('anomalies latch per tick and clear on read; muted suppresses the ingress-stall reason', () => {
		const now = { t: 10_000 };
		const led = makeLedger(now);
		const s = fakeSession();
		led.wrapSession(s);
		s.handleAudioFromClient(pcm(0.1));
		led.ingestHeartbeat(wireHb('nnnnnnnn', { ep: [[1, 'g', 0, 1200]], og: [0, 1500] }));
		const a1 = led.anomalySinceLastTick(true);
		assert.equal(a1.anomalous, true);
		assert.ok(a1.reasons.includes('capStalled'));
		assert.ok(a1.reasons.some((r) => r.startsWith('episodes:')));
		// same episode re-sent (idempotent window) → no NEW episode anomaly
		led.ingestHeartbeat(wireHb('nnnnnnnn', { q: 1, ep: [[1, 'g', 0, 1200]], og: [0, 3500] }));
		const a2 = led.anomalySinceLastTick(true);
		assert.ok(!a2.reasons.some((r) => r.startsWith('episodes:')), 'dedup by episode id');
		now.t += 6000;
		led.ingestHeartbeat(wireHb('nnnnnnnn', { q: 2, mu: 1 }));
		const a3 = led.anomalySinceLastTick(true);
		assert.ok(!a3.reasons.includes('ingress-stalled'), 'muted client is not an ingress stall');
	});

	it('sendSkipped delta in any heartbeat is an anomaly', () => {
		const led = makeLedger({ t: 10_000 });
		led.ingestHeartbeat(wireHb('nnnnnnnn', { c: [47, 64000, 5, 0, 0, 0, 0, 0] }));
		assert.ok(led.anomalySinceLastTick(true).reasons.includes('sendSkipped'));
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
		assert.ok(led.anomalySinceLastTick(true).reasons.includes('persistSkipped'));
	});

	it('healthSegments renders the upgraded line fields', () => {
		const now = { t: 10_000 };
		const led = makeLedger(now);
		const s = fakeSession();
		led.wrapSession(s);
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
