/**
 * STTProvider for Gemini Transcribe Live (`gemini-3.5-transcribe-live`),
 * used as bodhi's `whisperProvider` so meeting mode can run on bodhi's
 * dictation (transcription) mode without an OpenAI key.
 *
 * Protocol: https://ai.google.dev/gemini-api/docs/live-api/live-transcribe
 *   setup → { model, generationConfig.responseModalities:["TEXT"], inputAudioTranscription }
 *   audio → realtimeInput.audio { data, mimeType:"audio/pcm;rate=16000" }
 *   out   → serverContent.interimInputTranscription.text (partial)
 *           serverContent.inputTranscription.text        (final)
 *
 * A Transcribe Live session lasts at most 10 minutes, so the provider rotates:
 * shortly before the limit it opens a new socket, keeps feeding the old one
 * until the new one is ready and the speaker pauses, then switches over and sends
 * `audioStreamEnd` to the old one so its last utterance is finalized before it
 * closes. Every audio chunk goes to exactly one socket — nothing is fed twice,
 * and nothing is dropped while the new socket connects.
 */

import WebSocket from 'ws';
import type { STTProvider } from 'bodhi-realtime-agent';

type STTAudioConfig = Parameters<STTProvider['configure']>[0];

export interface GeminiLiveTranscribeConfig {
	apiKey: string;
	model?: string;
	/** BCP-47 hints; empty = auto-detect (handles mixed Chinese/English). */
	languageCodes?: string[];
	/** Up to 1000 names/terms the model should spell correctly. */
	customVocabulary?: string[];
	mode?: 'VERBATIM' | 'SMART';
	/** Rotate before the server's 10-minute cap. */
	rotateAfterMs?: number;
	/** Longest wait for a pause before handing over anyway. */
	pauseWaitMs?: number;
	/** How long the old socket stays open after `audioStreamEnd`. */
	drainMs?: number;
	/** Test seam. */
	createSocket?: (url: string) => WebSocketLike;
	log?: (msg: string) => void;
}

export interface WebSocketLike {
	readyState: number;
	send(data: string): void;
	close(): void;
	on(event: 'open' | 'message' | 'close' | 'error', fn: (...args: any[]) => void): void;
}

const WS_URL = 'wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent';
const TARGET_RATE = 16000;
const OPEN = 1;
const STOP_DRAIN_MS = 500;
const QUIET_RMS = 300;
const PAUSE_MS = 250;

export function rmsPcm16(pcm: Buffer): number {
	const n = Math.floor(pcm.length / 2);
	if (n === 0) return 0;
	let sum = 0;
	for (let i = 0; i < n; i++) {
		const v = pcm.readInt16LE(i * 2);
		sum += v * v;
	}
	return Math.sqrt(sum / n);
}
/** ~30 s of 16 kHz PCM16 held while no socket is ready yet. */
const MAX_PENDING_BYTES = 960_000;

/** Linear-interpolation resample of mono PCM16 (little-endian). */
export function resamplePcm16(input: Buffer, fromRate: number, toRate: number): Buffer {
	if (fromRate === toRate) return input;
	const inSamples = Math.floor(input.length / 2);
	const outSamples = Math.floor((inSamples * toRate) / fromRate);
	const out = Buffer.alloc(outSamples * 2);
	const ratio = fromRate / toRate;
	for (let i = 0; i < outSamples; i++) {
		const pos = i * ratio;
		const i0 = Math.floor(pos);
		const i1 = Math.min(i0 + 1, inSamples - 1);
		const frac = pos - i0;
		const s = input.readInt16LE(i0 * 2) * (1 - frac) + input.readInt16LE(i1 * 2) * frac;
		out.writeInt16LE(Math.max(-32768, Math.min(32767, Math.round(s))), i * 2);
	}
	return out;
}

interface Conn {
	ws: WebSocketLike;
	ready: boolean;
	id: number;
}

export class GeminiLiveTranscribeSTTProvider implements STTProvider {
	onTranscript?: (text: string, turnId: number | undefined) => void;
	onPartialTranscript?: (text: string) => void;

	private readonly cfg: Required<Omit<GeminiLiveTranscribeConfig, 'createSocket' | 'log' | 'customVocabulary'>> & Pick<GeminiLiveTranscribeConfig, 'customVocabulary'>;
	private readonly createSocket: (url: string) => WebSocketLike;
	private readonly log: (msg: string) => void;
	private inputRate = 24000;
	private running = false;
	/** The socket audio is currently fed to. */
	private active: Conn | null = null;
	/** A socket that is connecting to replace `active`. */
	private next: Conn | null = null;
	private pending: Buffer[] = [];
	private pendingBytes = 0;
	private rotateTimer: NodeJS.Timeout | null = null;
	private connSeq = 0;
	private switchDeadline = 0;
	private quietMs = 0;

	constructor(config: GeminiLiveTranscribeConfig) {
		this.cfg = {
			apiKey: config.apiKey,
			model: config.model ?? 'gemini-3.5-transcribe-live',
			languageCodes: config.languageCodes ?? [],
			customVocabulary: config.customVocabulary,
			mode: config.mode ?? 'VERBATIM',
			rotateAfterMs: config.rotateAfterMs ?? 9 * 60_000,
			drainMs: config.drainMs ?? 5_000,
			pauseWaitMs: config.pauseWaitMs ?? 30_000,
		};
		this.createSocket = config.createSocket ?? ((url) => new WebSocket(url) as unknown as WebSocketLike);
		this.log = config.log ?? (() => {});
	}

	configure(audio: STTAudioConfig): void {
		if (audio.bitDepth !== 16 || audio.channels !== 1) {
			throw new Error(`GeminiLiveTranscribeSTTProvider needs PCM16 mono, got ${audio.bitDepth}-bit ${audio.channels}ch`);
		}
		this.inputRate = audio.sampleRate;
	}

	async start(): Promise<void> {
		if (this.running) return; // idempotent (prewarm + enter both call it)
		this.running = true;
		this.active = this.open();
		this.scheduleRotation();
	}

	async stop(): Promise<void> {
		if (!this.running) return;
		this.running = false;
		if (this.rotateTimer) clearTimeout(this.rotateTimer);
		this.rotateTimer = null;
		this.flushPendingTo(this.active);
		this.pending = [];
		this.pendingBytes = 0;
		if (this.next) this.next.ws.close();
		this.next = null;
		const last = this.active;
		this.active = null;
		// Short drain: the exit phrase is itself a final transcript, so earlier speech is already in.
		if (last) await this.drainAndClose(last, Math.min(this.cfg.drainMs, STOP_DRAIN_MS));
	}

	feedAudio(base64Pcm: string): void {
		if (!this.running) return;
		const pcm = resamplePcm16(Buffer.from(base64Pcm, 'base64'), this.inputRate, TARGET_RATE);
		this.maybeSwitchAtPause(pcm);
		const conn = this.active;
		if (conn?.ready && conn.ws.readyState === OPEN) {
			this.flushPendingTo(conn);
			this.sendAudio(conn, pcm);
			return;
		}
		this.pending.push(pcm);
		this.pendingBytes += pcm.length;
		while (this.pendingBytes > MAX_PENDING_BYTES && this.pending.length > 1) {
			this.pendingBytes -= this.pending.shift()!.length;
		}
	}

	// Streaming provider with server-side VAD: turn signals are not needed.
	commit(_turnId: number): void {}
	handleInterrupted(): void {}
	handleTurnComplete(): void {}

	private open(): Conn {
		const conn: Conn = { ws: this.createSocket(`${WS_URL}?key=${this.cfg.apiKey}`), ready: false, id: ++this.connSeq };
		const inputAudioTranscription: Record<string, unknown> = { languageCodes: this.cfg.languageCodes, mode: this.cfg.mode };
		if (this.cfg.customVocabulary?.length) inputAudioTranscription.customVocabulary = this.cfg.customVocabulary.slice(0, 1000);
		conn.ws.on('open', () => {
			conn.ws.send(JSON.stringify({
				setup: {
					model: `models/${this.cfg.model}`,
					generationConfig: { responseModalities: ['TEXT'] },
					inputAudioTranscription,
				},
			}));
		});
		conn.ws.on('message', (raw: unknown) => this.onMessage(conn, raw));
		conn.ws.on('error', (err: Error) => this.log(`[Transcribe#${conn.id}] error: ${err?.message ?? err}`));
		conn.ws.on('close', (code: number, reason: unknown) => {
			this.log(`[Transcribe#${conn.id}] closed ${code ?? ''} ${String(reason ?? '')}`.trim());
			// Unexpected loss of the socket we feed: reconnect, buffering meanwhile.
			if (this.running && this.active === conn) {
				this.active = this.next ?? this.open();
				this.next = null;
			}
			if (this.next === conn) this.next = null;
		});
		return conn;
	}

	private onMessage(conn: Conn, raw: unknown): void {
		let msg: any;
		try {
			msg = JSON.parse(typeof raw === 'string' ? raw : Buffer.from(raw as Buffer).toString('utf-8'));
		} catch {
			return;
		}
		if (msg.setupComplete !== undefined) {
			conn.ready = true;
			this.log(`[Transcribe#${conn.id}] ready`);
			if (conn === this.next && !this.active?.ready) this.switchTo(conn);
			else if (conn === this.active) this.flushPendingTo(conn);
			return;
		}
		const sc = msg.serverContent;
		if (!sc) return;
		const partial = sc.interimInputTranscription?.text;
		if (typeof partial === 'string' && partial) this.onPartialTranscript?.(partial);
		const final = sc.inputTranscription?.text;
		if (typeof final === 'string' && final.trim()) this.onTranscript?.(final.trim(), undefined);
	}

	private scheduleRotation(): void {
		if (this.rotateTimer) clearTimeout(this.rotateTimer);
		this.rotateTimer = setTimeout(() => {
			if (!this.running) return;
			this.log('[Transcribe] rotating session before the 10-minute limit');
			this.next = this.open();
			this.switchDeadline = Date.now() + this.cfg.pauseWaitMs;
			this.quietMs = 0;
		}, this.cfg.rotateAfterMs);
		this.rotateTimer.unref?.();
	}

	/** Hand over to the ready next session during a pause, so no word is split across sessions. */
	private maybeSwitchAtPause(pcm: Buffer): void {
		const next = this.next;
		if (!next?.ready || next.ws.readyState !== OPEN) return;
		this.quietMs = rmsPcm16(pcm) < QUIET_RMS ? this.quietMs + (pcm.length / 2 / TARGET_RATE) * 1000 : 0;
		if (this.quietMs >= PAUSE_MS || Date.now() >= this.switchDeadline) {
			this.log(`[Transcribe#${next.id}] taking over (${this.quietMs >= PAUSE_MS ? 'pause' : 'deadline'})`);
			this.switchTo(next);
		}
	}

	private switchTo(conn: Conn): void {
		const old = this.active;
		this.active = conn;
		this.next = null;
		this.flushPendingTo(conn);
		this.scheduleRotation();
		if (old && old !== conn) void this.drainAndClose(old);
	}

	private async drainAndClose(conn: Conn, waitMs = this.cfg.drainMs): Promise<void> {
		try {
			if (conn.ws.readyState === OPEN) conn.ws.send(JSON.stringify({ realtimeInput: { audioStreamEnd: true } }));
		} catch {}
		await new Promise((r) => setTimeout(r, waitMs));
		try { conn.ws.close(); } catch {}
	}

	private flushPendingTo(conn: Conn | null): void {
		if (!conn?.ready || conn.ws.readyState !== OPEN || this.pending.length === 0) return;
		const chunks = this.pending;
		this.pending = [];
		this.pendingBytes = 0;
		for (const c of chunks) this.sendAudio(conn, c);
	}

	private sendAudio(conn: Conn, pcm: Buffer): void {
		conn.ws.send(JSON.stringify({ realtimeInput: { audio: { data: pcm.toString('base64'), mimeType: `audio/pcm;rate=${TARGET_RATE}` } } }));
	}
}
