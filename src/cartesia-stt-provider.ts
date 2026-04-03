/**
 * Cartesia ink-whisper STT provider — drop-in replacement for GeminiBatchSTTProvider.
 *
 * Implements bodhi-realtime-agent's STTProvider interface using the Cartesia
 * bytes endpoint (batch transcription). Audio is buffered via feedAudio(),
 * then transcribed when commit() is called.
 *
 * Optimized for: variable-length chunks, background noise, telephony artifacts,
 * accents, and domain-specific terminology.
 */

import type { STTProvider, STTAudioConfig } from 'bodhi-realtime-agent';

const ts = () => new Date().toLocaleTimeString('en-US', { hour12: false });
const MAX_BUFFER_BYTES = 25 * 1024 * 1024; // 25 MB safety cap

export interface CartesiaSTTConfig {
	apiKey: string;
	model?: string;
	/** Cartesia API version header. */
	apiVersion?: string;
}

export class CartesiaSTTProvider implements STTProvider {
	private readonly apiKey: string;
	private readonly model: string;
	private readonly apiVersion: string;
	private sampleRate = 16000;
	private audioChunks: string[] = [];
	private bufferBytes = 0;
	private wasInterrupted = false;

	onTranscript?: (text: string, turnId: number | undefined) => void;
	onPartialTranscript?: (text: string) => void;

	constructor(config: CartesiaSTTConfig) {
		this.apiKey = config.apiKey;
		this.model = config.model || 'ink-whisper';
		this.apiVersion = config.apiVersion || '2025-04-16';
	}

	configure(audio: STTAudioConfig): void {
		if (audio.bitDepth !== 16) {
			throw new Error(`CartesiaSTTProvider requires bitDepth=16, got ${audio.bitDepth}`);
		}
		if (audio.channels !== 1) {
			throw new Error(`CartesiaSTTProvider requires channels=1, got ${audio.channels}`);
		}
		this.sampleRate = audio.sampleRate;
	}

	async start(): Promise<void> {
		console.log(`${ts()} [CartesiaSTT] Started (model: ${this.model}, sampleRate: ${this.sampleRate})`);
	}

	async stop(): Promise<void> {
		this.audioChunks = [];
		this.bufferBytes = 0;
		console.log(`${ts()} [CartesiaSTT] Stopped`);
	}

	feedAudio(base64Pcm: string): void {
		const chunkBytes = Math.ceil(base64Pcm.length * 0.75);
		// FIFO eviction: drop oldest chunks to make room (preserves most recent speech)
		while (this.bufferBytes + chunkBytes > MAX_BUFFER_BYTES && this.audioChunks.length > 0) {
			const dropped = this.audioChunks.shift();
			if (dropped) this.bufferBytes -= Math.ceil(dropped.length * 0.75);
		}
		this.audioChunks.push(base64Pcm);
		this.bufferBytes += chunkBytes;
	}

	commit(turnId: number): void {
		if (this.audioChunks.length === 0) return;

		const chunks = this.audioChunks;
		this.audioChunks = [];
		this.bufferBytes = 0;

		// Concatenate all buffered PCM chunks
		const allAudio = Buffer.concat(chunks.map(b64 => Buffer.from(b64, 'base64')));

		if (allAudio.length < 320) return; // skip near-empty buffers (< 10ms at 16kHz)

		fetch('https://api.cartesia.ai/stt/bytes', {
			method: 'POST',
			headers: {
				'X-API-Key': this.apiKey,
				'Cartesia-Version': this.apiVersion,
				'Content-Type': 'audio/pcm',
				'Sample-Rate': String(this.sampleRate),
				'Encoding': 'pcm_s16le',
				'Language': 'en',
			},
			body: allAudio,
		})
			.then(async res => {
				if (!res.ok) {
					const body = await res.text().catch(() => '');
					throw new Error(`HTTP ${res.status}: ${body.slice(0, 200)}`);
				}
				return res.json();
			})
			.then((data: any) => {
				const text = data?.text?.trim();
				if (text && this.onTranscript) {
					console.log(`${ts()} [CartesiaSTT] Transcript (turn ${turnId}): "${text.slice(0, 80)}${text.length > 80 ? '...' : ''}"`);
					this.onTranscript(text, turnId);
				}
			})
			.catch(err => {
				console.error(`${ts()} [CartesiaSTT] Transcription error:`, err.message);
			});
	}

	handleInterrupted(): void {
		this.wasInterrupted = true;
		// Preserve buffer — audio will be included in next commit()
	}

	handleTurnComplete(): void {
		if (!this.wasInterrupted) {
			this.audioChunks = [];
			this.bufferBytes = 0;
		}
		this.wasInterrupted = false;
	}
}
