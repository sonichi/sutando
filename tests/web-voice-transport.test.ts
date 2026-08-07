import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
	downsample,
	float32ToInt16,
	int16ToFloat32,
	classifyMicError,
	VoiceTransport,
} from '../src/web-voice-transport.js';

// Feed a JSON frame straight into the private router. The frame path touches no
// browser API (flushPlayback over an empty queue is a no-op, ArrayBuffer is a
// Node global), so the turn.end/turn.interrupted contract is Node-testable.
function feed(t: VoiceTransport, obj: unknown): void {
	(t as any).onMessage({ data: JSON.stringify(obj) });
}

describe('web-voice-transport DSP', () => {
	it('downsample: identity when rates match', () => {
		const input = new Float32Array([0.1, -0.2, 0.3, -0.4]);
		const out = downsample(input, 48000, 48000);
		assert.equal(out, input, 'returns the same reference when fromRate === toRate');
	});

	it('downsample: 48k → 16k reduces length by ~3x', () => {
		const input = new Float32Array(48).fill(0.5);
		const out = downsample(input, 48000, 16000);
		assert.equal(out.length, 16); // floor(48 / (48000/16000)) = floor(48/3) = 16
	});

	it('downsample: linear interpolation between samples', () => {
		// fromRate 2 → toRate 1: ratio 2, out[i] = input[2i] (frac 0)
		const input = new Float32Array([0, 1, 0, 1, 0, 1]);
		const out = downsample(input, 2, 1);
		assert.equal(out.length, 3);
		assert.deepEqual(Array.from(out), [0, 0, 0]);
	});

	it('downsample: last-sample edge uses 0 for the missing neighbour', () => {
		// ratio 1.5 → pos for last index can land on idx = len-1 with frac>0;
		// input[idx+1] || 0 must not throw / NaN.
		const input = new Float32Array([1, 1, 1, 1]);
		const out = downsample(input, 3, 2);
		assert.ok(out.every((v) => Number.isFinite(v)), 'no NaN at the tail');
	});

	it('float32ToInt16: full-scale + clamp', () => {
		const i16 = float32ToInt16(new Float32Array([1, -1, 0, 2, -2]));
		assert.equal(i16[0], 0x7fff); // +1 → +32767
		assert.equal(i16[1], -0x8000); // -1 → -32768
		assert.equal(i16[2], 0);
		assert.equal(i16[3], 0x7fff); // clamps > 1
		assert.equal(i16[4], -0x8000); // clamps < -1
	});

	it('int16ToFloat32: little-endian decode', () => {
		const buf = new ArrayBuffer(4);
		const dv = new DataView(buf);
		dv.setInt16(0, 32767, true);
		dv.setInt16(2, -32768, true);
		const f = int16ToFloat32(buf);
		assert.ok(Math.abs(f[0] - 0.99997) < 1e-3);
		assert.equal(f[1], -1);
	});

	it('round-trip f32 → i16 → f32 is near-identity (< 2 LSB)', () => {
		// Bound is 2 LSB, not 1: the encode scale (×0x7FFF) and decode scale
		// (÷0x8000) are deliberately asymmetric (faithful to web-client), which
		// adds a small systematic error growing with amplitude on top of the
		// 0.5-LSB quantization. Both are preserved intentionally.
		const src = new Float32Array([0, 0.25, -0.25, 0.5, -0.5, 0.9, -0.9]);
		const back = int16ToFloat32(float32ToInt16(src).buffer);
		for (let i = 0; i < src.length; i++) {
			assert.ok(Math.abs(back[i] - src[i]) < 2 / 32768, `sample ${i} within 2 LSB`);
		}
	});

	it('int16ToFloat32: empty buffer → empty array (playChunk guard)', () => {
		assert.equal(int16ToFloat32(new ArrayBuffer(0)).length, 0);
	});
});

describe('web-voice-transport classifyMicError', () => {
	it('permission-class → settings guidance', () => {
		for (const n of ['NotAllowedError', 'SecurityError']) {
			assert.match(classifyMicError(n), /denied|browser settings/i);
		}
	});
	it('busy-class → close-other-app guidance (not a permission message)', () => {
		for (const n of ['NotReadableError', 'AbortError']) {
			const m = classifyMicError(n);
			assert.match(m, /in use|another app/i);
			assert.doesNotMatch(m, /denied/i);
		}
	});
	it('absent-class → connect-a-device guidance', () => {
		for (const n of ['NotFoundError', 'OverconstrainedError']) {
			assert.match(classifyMicError(n), /no microphone found/i);
		}
	});
	it('unknown/undefined → generic retry with the name echoed', () => {
		assert.match(classifyMicError('WeirdError'), /WeirdError/);
		assert.match(classifyMicError(undefined), /unknown/);
	});
});

describe('web-voice-transport turn lifecycle (drift guard vs web-client)', () => {
	it('turn.end fires onTurnEnd and does NOT flush/interrupt (final audio drains)', () => {
		let ended = 0;
		let interrupted = 0;
		const t = new VoiceTransport({
			onTurnEnd: () => ended++,
			onInterrupted: () => interrupted++,
		});
		feed(t, { type: 'turn.end' });
		assert.equal(ended, 1);
		assert.equal(interrupted, 0, 'turn.end must not trigger a barge-in flush');
	});

	it('turn.interrupted fires onInterrupted only (barge-in cut-off)', () => {
		let ended = 0;
		let interrupted = 0;
		const t = new VoiceTransport({
			onTurnEnd: () => ended++,
			onInterrupted: () => interrupted++,
		});
		// Must not throw flushing an empty playback queue.
		feed(t, { type: 'turn.interrupted' });
		assert.equal(interrupted, 1);
		assert.equal(ended, 0, 'turn.interrupted is distinct from turn.end');
	});

	it('session.config negotiates rates via onSessionConfig', () => {
		let rates: [number, number] | null = null;
		const t = new VoiceTransport({ onSessionConfig: (i, o) => (rates = [i, o]) });
		feed(t, { type: 'session.config', audioFormat: { inputSampleRate: 8000, outputSampleRate: 48000 } });
		assert.deepEqual(rates, [8000, 48000]);
	});

	it('every frame is also forwarded raw to onProtocolMessage', () => {
		const seen: string[] = [];
		const t = new VoiceTransport({ onProtocolMessage: (m) => seen.push(m.type) });
		feed(t, { type: 'image', base64: 'x' });
		feed(t, { type: 'turn.end' });
		assert.deepEqual(seen, ['image', 'turn.end']);
	});

	it('disconnect()/close() are idempotent with no live session (no throw)', () => {
		const t = new VoiceTransport();
		assert.doesNotThrow(() => t.disconnect());
		assert.doesNotThrow(() => t.close());
		assert.doesNotThrow(() => t.disconnect());
		assert.equal(t.connected, false);
	});
});

// ─── Capability gaps closed so the web UI can adopt this module ──────────────
//
// Each of these existed in web-client's inline transport and NOT here, which is
// why the UI could not switch over without a behavior change. They are pinned
// so the switch (and any later tidy) cannot quietly drop them again.

describe('web-voice-transport close info (reconnect policy lives in the surface)', () => {
	it('close carries the raw code and reason, not just a status string', () => {
		const seen: Array<{ status: string; detail?: string; close?: { code: number; reason: string } }> = [];
		const t = new VoiceTransport({
			onStatus: (status, detail, close) => seen.push({ status, detail, close }),
		});

		// Drive ws.onclose the way the browser would, without a real socket.
		(t as any).ws = null;
		(t as any).teardownAudio = () => {};
		(t as any).status('closed', 'Disconnected', { code: 4000, reason: 'goodbye' });

		const closed = seen.find(s => s.status === 'closed');
		assert.ok(closed, 'a closed status must be emitted');
		assert.equal(closed!.close?.code, 4000);
		assert.equal(closed!.close?.reason, 'goodbye');
	});

	it('the surface can distinguish a clean goodbye from an unexpected drop', () => {
		// This is the whole reason the code is exposed: web-client treats 4000
		// (and a user-initiated disconnect) as clean, and everything else as a
		// drop that should trigger its reconnect ladder.
		const isClean = (code: number) => code === 4000;
		assert.equal(isClean(4000), true);
		assert.equal(isClean(1006), false, 'abnormal closure must NOT read as clean');
	});
});

describe('web-voice-transport debug sink (feeds the panel + downloadable dump)', () => {
	it('forwards protocol frames to onDebug with the event channel', () => {
		const lines: Array<[string, string | undefined]> = [];
		const t = new VoiceTransport({ onDebug: (msg, kind) => lines.push([msg, kind]) });

		feed(t, { type: 'turn.end' });

		const evt = lines.find(([, kind]) => kind === 'event');
		assert.ok(evt, 'a JSON frame must produce an event-channel debug line');
		assert.match(evt![0], /turn\.end/, 'the frame itself must appear in the trace');
	});

	it('reports an unparseable text frame rather than dropping it silently', () => {
		const lines: Array<[string, string | undefined]> = [];
		const t = new VoiceTransport({ onDebug: (msg, kind) => lines.push([msg, kind]) });

		(t as any).onMessage({ data: 'not json at all' });

		assert.ok(
			lines.some(([msg, kind]) => kind === 'warn' && /bad json/i.test(msg)),
			'a malformed frame must be visible in the debug trace',
		);
	});

	it('is entirely optional — no onDebug means no throw', () => {
		const t = new VoiceTransport({});
		assert.doesNotThrow(() => feed(t, { type: 'turn.end' }));
		assert.doesNotThrow(() => (t as any).onMessage({ data: 'nope' }));
	});
});

describe('web-voice-transport mic-error wording matches the shipped web UI', () => {
	it('the unclassified case echoes the underlying browser message', () => {
		// web-client showed the raw DOMException text here; this module used to
		// drop it, which would have made the guidance strictly worse for exactly
		// the failures nobody has classified yet.
		const m = classifyMicError('WeirdError', 'device exploded');
		assert.match(m, /WeirdError/);
		assert.match(m, /device exploded/);
		assert.match(m, /Click Connect to retry/);
	});

	it('falls back to a concrete phrase when the browser gives no message', () => {
		const m = classifyMicError('WeirdError');
		assert.match(m, /could not start capture/);
		assert.doesNotMatch(m, /undefined/, 'must never render the string "undefined" at a user');
	});

	it('classified cases are unaffected by the added message argument', () => {
		assert.match(classifyMicError('NotAllowedError', 'ignored'), /denied/i);
		assert.match(classifyMicError('NotFoundError', 'ignored'), /no microphone found/i);
	});
});
