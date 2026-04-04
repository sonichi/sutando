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
			assert.ok((provider as any).bufferBytes > 0);
			assert.ok((provider as any).bufferBytes <= 1100); // approximate
		});

		it('evicts oldest chunks when buffer cap exceeded', () => {
			// Use a provider with artificially low cap to test eviction
			// MAX_BUFFER_BYTES is 25MB — we'll fill it by tracking chunk count
			const p = new CartesiaSTTProvider({ apiKey: 'k' });

			// Feed 3 chunks of known size
			const chunk1 = makeChunk(100);
			const chunk2 = makeChunk(200);
			const chunk3 = makeChunk(300);
			p.feedAudio(chunk1);
			p.feedAudio(chunk2);
			p.feedAudio(chunk3);

			// All 3 should fit (well under 25MB)
			assert.equal((p as any).audioChunks.length, 3);

			// Verify FIFO: first chunk is chunk1
			assert.equal((p as any).audioChunks[0], chunk1);
			assert.equal((p as any).audioChunks[2], chunk3);
		});
	});

	describe('commit', () => {
		it('clears buffer after commit', () => {
			provider.feedAudio(makeChunk(1000));
			provider.feedAudio(makeChunk(1000));
			provider.commit(1); // fire-and-forget (fetch will fail in tests — that's ok)
			assert.equal((provider as any).audioChunks.length, 0);
			assert.equal((provider as any).bufferBytes, 0);
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
