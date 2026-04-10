import { describe, it, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { CartesiaSTTProvider } from '../src/cartesia-stt-provider.js';

/** Create a base64 PCM chunk of given decoded byte size */
function makeChunk(decodedBytes: number): string {
	return Buffer.alloc(decodedBytes).toString('base64');
}

describe('CartesiaSTTProvider', () => {
	let provider: CartesiaSTTProvider;

	beforeEach(() => {
		provider = new CartesiaSTTProvider({ apiKey: 'test-key' });
	});

	describe('constructor', () => {
		it('defaults to ink-whisper model', () => {
			assert.equal((provider as any).model, 'ink-whisper');
		});

		it('accepts custom model', () => {
			const p = new CartesiaSTTProvider({ apiKey: 'k', model: 'custom-v2' });
			assert.equal((p as any).model, 'custom-v2');
		});

		it('defaults apiVersion', () => {
			assert.equal((provider as any).apiVersion, '2025-04-16');
		});
	});

	describe('configure', () => {
		it('accepts valid audio config', () => {
			assert.doesNotThrow(() => {
				provider.configure({ sampleRate: 16000, bitDepth: 16, channels: 1 });
			});
		});

		it('stores the sample rate', () => {
			provider.configure({ sampleRate: 24000, bitDepth: 16, channels: 1 });
			assert.equal((provider as any).sampleRate, 24000);
		});

		it('rejects bitDepth !== 16', () => {
			assert.throws(
				() => provider.configure({ sampleRate: 16000, bitDepth: 8, channels: 1 }),
				/bitDepth=16, got 8/,
			);
		});

		it('rejects channels !== 1', () => {
			assert.throws(
				() => provider.configure({ sampleRate: 16000, bitDepth: 16, channels: 2 }),
				/channels=1, got 2/,
			);
		});
	});

	describe('feedAudio', () => {
		it('buffers audio chunks', () => {
			provider.feedAudio(makeChunk(100));
			provider.feedAudio(makeChunk(200));
			assert.equal((provider as any).audioChunks.length, 2);
		});

		it('tracks approximate buffer bytes', () => {
			const chunk = makeChunk(1000); // 1000 decoded bytes
			provider.feedAudio(chunk);
			// base64 length * 0.75 ≈ decoded size
			assert.ok(provider.currentBufferBytes > 0);
			assert.equal(provider.currentBufferBytes, 1000); // exact: 500+500
		});

		it('retains all chunks when under cap', () => {
			const p = new CartesiaSTTProvider({ apiKey: 'k' });
			const chunk1 = makeChunk(100);
			const chunk2 = makeChunk(200);
			const chunk3 = makeChunk(300);
			p.feedAudio(chunk1);
			p.feedAudio(chunk2);
			p.feedAudio(chunk3);
			assert.equal(p.chunkCount, 3);
			assert.equal(p.currentBufferBytes, 600); // 100+200+300
		});

		it('evicts oldest chunks when buffer cap is hit', () => {
			const p = new CartesiaSTTProvider({ apiKey: 'k' });
			// MAX_BUFFER_BYTES is 25MB. Fill with 10MB chunks to trigger eviction on 3rd.
			const tenMB = 10 * 1024 * 1024;
			const big1 = makeChunk(tenMB);
			const big2 = makeChunk(tenMB);
			p.feedAudio(big1);
			p.feedAudio(big2);
			assert.equal(p.chunkCount, 2);
			assert.equal(p.currentBufferBytes, tenMB * 2);

			// Third 10MB chunk exceeds 25MB cap — oldest should be evicted
			const big3 = makeChunk(tenMB);
			p.feedAudio(big3);
			assert.equal(p.chunkCount, 2); // big1 evicted, big2+big3 remain
			assert.equal(p.currentBufferBytes, tenMB * 2);
		});

		it('drops single chunk exceeding MAX_BUFFER_BYTES', () => {
			const p = new CartesiaSTTProvider({ apiKey: 'k' });
			const huge = makeChunk(26 * 1024 * 1024); // 26MB > 25MB cap
			p.feedAudio(huge);
			assert.equal(p.chunkCount, 0);
			assert.equal(p.currentBufferBytes, 0);
		});
	});

	describe('commit', () => {
		it('clears buffer after commit', () => {
			provider.feedAudio(makeChunk(1000));
			provider.feedAudio(makeChunk(1000));
			provider.commit(1); // fire-and-forget (fetch will fail in tests — that's ok)
			assert.equal(provider.chunkCount, 0);
			assert.equal(provider.currentBufferBytes, 0);
		});

		it('skips empty buffer', () => {
			let called = false;
			provider.onTranscript = () => { called = true; };
			provider.commit(1);
			assert.equal(called, false);
		});

		it('skips near-empty buffer (< 320 decoded bytes)', () => {
			// Feed a tiny chunk (< 320 bytes decoded)
			provider.feedAudio(makeChunk(100));
			provider.commit(1);
			// Buffer should be cleared by commit even if fetch is skipped
			assert.equal((provider as any).audioChunks.length, 0);
		});
	});

	describe('handleInterrupted + handleTurnComplete', () => {
		it('handleTurnComplete clears buffer on normal completion', () => {
			provider.feedAudio(makeChunk(500));
			assert.equal((provider as any).audioChunks.length, 1);

			provider.handleTurnComplete();
			assert.equal((provider as any).audioChunks.length, 0);
			assert.equal((provider as any).bufferBytes, 0);
		});

		it('handleInterrupted preserves buffer for next commit', () => {
			provider.feedAudio(makeChunk(500));
			provider.handleInterrupted();

			// handleTurnComplete after interrupt should NOT clear
			provider.handleTurnComplete();
			assert.equal((provider as any).audioChunks.length, 1);
		});

		it('resets interrupt flag after handleTurnComplete', () => {
			provider.feedAudio(makeChunk(500));
			provider.handleInterrupted();
			provider.handleTurnComplete(); // preserves buffer, resets flag

			// Second handleTurnComplete should clear (no interrupt)
			provider.handleTurnComplete();
			assert.equal((provider as any).audioChunks.length, 0);
		});

		it('interrupt without prior audio is harmless', () => {
			assert.doesNotThrow(() => {
				provider.handleInterrupted();
				provider.handleTurnComplete();
			});
		});
	});

	describe('stop', () => {
		it('clears all state including wasInterrupted', async () => {
			provider.feedAudio(makeChunk(500));
			provider.handleInterrupted();
			assert.equal((provider as any).wasInterrupted, true);

			await provider.stop();

			assert.equal((provider as any).audioChunks.length, 0);
			assert.equal((provider as any).bufferBytes, 0);
			assert.equal((provider as any).wasInterrupted, false);
		});

		it('handleTurnComplete after stop does not preserve stale interrupt', async () => {
			provider.feedAudio(makeChunk(500));
			provider.handleInterrupted();
			await provider.stop();

			provider.feedAudio(makeChunk(500));
			provider.handleTurnComplete();

			assert.equal((provider as any).audioChunks.length, 0);
		});
	});

	describe('commit after stop', () => {
		it('stopped flag is set after stop', async () => {
			await provider.stop();
			assert.equal((provider as any).stopped, true);
		});

		it('start resets the stopped flag', async () => {
			await provider.stop();
			await provider.start();
			assert.equal((provider as any).stopped, false);
		});
	});
});
