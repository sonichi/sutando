import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { createWavHeader } from '../src/cartesia-tts.js';

describe('createWavHeader', () => {
	it('produces a 44-byte header', () => {
		const header = createWavHeader(0, 24000, 1, 16);
		assert.equal(header.length, 44);
	});

	it('starts with RIFF marker', () => {
		const header = createWavHeader(1000, 24000, 1, 16);
		assert.equal(header.toString('ascii', 0, 4), 'RIFF');
	});

	it('has WAVE format marker at offset 8', () => {
		const header = createWavHeader(1000, 24000, 1, 16);
		assert.equal(header.toString('ascii', 8, 12), 'WAVE');
	});

	it('has fmt subchunk at offset 12', () => {
		const header = createWavHeader(1000, 24000, 1, 16);
		assert.equal(header.toString('ascii', 12, 16), 'fmt ');
	});

	it('has data subchunk at offset 36', () => {
		const header = createWavHeader(1000, 24000, 1, 16);
		assert.equal(header.toString('ascii', 36, 40), 'data');
	});

	it('sets RIFF chunk size to 36 + dataSize', () => {
		const header = createWavHeader(48000, 24000, 1, 16);
		assert.equal(header.readUInt32LE(4), 36 + 48000);
	});

	it('sets fmt chunk size to 16 (PCM)', () => {
		const header = createWavHeader(1000, 24000, 1, 16);
		assert.equal(header.readUInt32LE(16), 16);
	});

	it('sets audio format to 1 (PCM)', () => {
		const header = createWavHeader(1000, 24000, 1, 16);
		assert.equal(header.readUInt16LE(20), 1);
	});

	it('sets correct channel count', () => {
		const mono = createWavHeader(1000, 24000, 1, 16);
		assert.equal(mono.readUInt16LE(22), 1);

		const stereo = createWavHeader(1000, 24000, 2, 16);
		assert.equal(stereo.readUInt16LE(22), 2);
	});

	it('sets correct sample rate', () => {
		const h1 = createWavHeader(1000, 24000, 1, 16);
		assert.equal(h1.readUInt32LE(24), 24000);

		const h2 = createWavHeader(1000, 44100, 1, 16);
		assert.equal(h2.readUInt32LE(24), 44100);
	});

	it('sets correct byte rate (sampleRate * channels * bitDepth / 8)', () => {
		// 24kHz, mono, 16-bit → 24000 * 1 * 2 = 48000
		const mono = createWavHeader(1000, 24000, 1, 16);
		assert.equal(mono.readUInt32LE(28), 48000);

		// 44.1kHz, stereo, 16-bit → 44100 * 2 * 2 = 176400
		const stereo = createWavHeader(1000, 44100, 2, 16);
		assert.equal(stereo.readUInt32LE(28), 176400);
	});

	it('sets correct block align (channels * bitDepth / 8)', () => {
		const mono = createWavHeader(1000, 24000, 1, 16);
		assert.equal(mono.readUInt16LE(32), 2); // 1 * 16 / 8

		const stereo = createWavHeader(1000, 24000, 2, 16);
		assert.equal(stereo.readUInt16LE(32), 4); // 2 * 16 / 8
	});

	it('sets correct bits per sample', () => {
		const header = createWavHeader(1000, 24000, 1, 16);
		assert.equal(header.readUInt16LE(34), 16);
	});

	it('sets data chunk size to dataSize', () => {
		const header = createWavHeader(96000, 24000, 1, 16);
		assert.equal(header.readUInt32LE(40), 96000);
	});

	it('produces valid header for 1 second of 24kHz mono 16-bit audio', () => {
		// 1 second = 24000 samples * 2 bytes = 48000 bytes
		const dataSize = 48000;
		const header = createWavHeader(dataSize, 24000, 1, 16);

		assert.equal(header.readUInt32LE(4), 36 + dataSize);  // RIFF size
		assert.equal(header.readUInt32LE(24), 24000);          // sample rate
		assert.equal(header.readUInt32LE(28), 48000);          // byte rate
		assert.equal(header.readUInt16LE(32), 2);              // block align
		assert.equal(header.readUInt32LE(40), dataSize);       // data size
	});

	it('handles zero-length data', () => {
		const header = createWavHeader(0, 24000, 1, 16);
		assert.equal(header.readUInt32LE(4), 36);  // RIFF size = 36 + 0
		assert.equal(header.readUInt32LE(40), 0);  // data size = 0
	});
});

describe('sentence splitting', () => {
	function splitSentences(text: string): string[] {
		const matched = text.match(/[^.!?]+[.!?]+/g) || [];
		const matchedText = matched.join('');
		const tail = text.slice(matchedText.length).trim();
		return matched.length > 0
			? (tail ? [...matched, tail] : matched)
			: [text];
	}

	it('handles text with no terminal punctuation', () => {
		assert.deepEqual(splitSentences('Hello world'), ['Hello world']);
	});

	it('captures trailing text without punctuation', () => {
		const result = splitSentences('Hello world. Testing');
		assert.equal(result.length, 2);
		assert.ok(result[0].includes('Hello world.'));
		assert.equal(result[1], 'Testing');
	});

	it('handles multiple sentences plus trailing text', () => {
		const result = splitSentences('One. Two! Three');
		assert.equal(result.length, 3);
		assert.equal(result[2], 'Three');
	});

	it('handles empty string', () => {
		assert.deepEqual(splitSentences(''), ['']);
	});

	it('handles text ending with punctuation (no tail)', () => {
		const result = splitSentences('Hello world.');
		assert.deepEqual(result, ['Hello world.']);
	});
});
