import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
	downsample,
	float32ToInt16,
	int16ToFloat32,
	classifyMicError,
} from '../src/web-voice-transport.js';

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
