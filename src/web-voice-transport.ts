/// <reference lib="dom" />
// ^ This module uses real browser types (AudioContext, WebSocket, getUserMedia).
//   The repo tsconfig is DOM-less (Node target); web-client.ts sidesteps that by
//   hiding its browser code in a template string. This module is real TS, so it
//   pulls the DOM lib in for its own compilation via the reference directive —
//   scoped here rather than adding DOM to the project lib (which would risk
//   Node/DOM global collisions, e.g. fetch/Response/Blob, across the codebase).

/**
 * web-voice-transport — the framework-agnostic browser voice-client CORE.
 *
 * This is the ONE canonical transport used by every Sutando voice surface:
 *   - the Sutando webUI (src/web-client.ts)
 *   - any embedded/host-app client that mounts a "call your agent" surface
 *
 * It owns exactly the parts that must be identical across surfaces — the audio
 * pipeline and the bodhi/Gemini WS wire protocol — and NOTHING that is UI:
 *
 *   TRANSPORT (here, universal)          SURFACE (per-UI, not here)
 *   ─────────────────────────────        ─────────────────────────────
 *   PCM DSP (down/up, i16<->f32)         transcript bubbles / chat DOM
 *   mic capture → send PCM               avatar / speaking animation
 *   recv PCM → gapless playback          image / video rendering
 *   WS connect + binary/JSON split       status text, stats panel
 *   session.config rate negotiation      Chrome interim-STT display
 *   turn.end barge-in (flush playback)
 *
 * The seam is the event callbacks: the transport plays audio itself and emits
 * every non-audio protocol frame to `onProtocolMessage` (plus typed shortcuts
 * for the common ones) so each surface renders in its own framework.
 *
 * The DSP functions are pure and exported standalone so they unit-test in Node
 * (see tests/web-voice-transport.test.ts). The class needs a browser (AudioContext,
 * getUserMedia, WebSocket) and is exercised by the surfaces + the happy-path spike.
 *
 * Transcribed faithfully from src/web-client.ts (the source of truth as of
 * 2026-07-08). The webUI-side dedup — pointing web-client.ts at this module
 * instead of its inline copy — is a follow-up that needs a browser-serve step;
 * until then a drift-guard test pins the two copies' DSP to equivalence.
 */

// ─── Pure PCM DSP (Node-testable) ─────────────────────────────

/** Linear-interpolation downsample. Identity when rates match. */
export function downsample(input: Float32Array, fromRate: number, toRate: number): Float32Array {
  if (fromRate === toRate) return input;
  const ratio = fromRate / toRate;
  const len = Math.floor(input.length / ratio);
  const out = new Float32Array(len);
  for (let i = 0; i < len; i++) {
    const pos = i * ratio;
    const idx = Math.floor(pos);
    const frac = pos - idx;
    out[i] = input[idx] * (1 - frac) + (input[idx + 1] || 0) * frac;
  }
  return out;
}

/** Float32 [-1,1] → Int16 PCM (asymmetric full-scale, matching web-client). */
export function float32ToInt16(f32: Float32Array): Int16Array {
  const i16 = new Int16Array(f32.length);
  for (let i = 0; i < f32.length; i++) {
    const s = Math.max(-1, Math.min(1, f32[i]));
    i16[i] = s < 0 ? (s * 0x8000) | 0 : (s * 0x7fff) | 0;
  }
  return i16;
}

/** Int16 little-endian PCM buffer → Float32 [-1,1]. */
export function int16ToFloat32(buf: ArrayBuffer): Float32Array {
  const view = new DataView(buf);
  const len = buf.byteLength / 2;
  const out = new Float32Array(len);
  for (let i = 0; i < len; i++) {
    out[i] = view.getInt16(i * 2, true) / 32768;
  }
  return out;
}

/**
 * Human-friendly microphone-error classification. Not every failure is a
 * permission denial — name the real cause so the user isn't sent to "browser
 * settings" when the mic is merely busy or absent. (Verbatim from web-client.)
 *
 * `message` is the underlying `DOMException.message`. The default branch echoes
 * it because an unclassified failure is exactly the case where the raw browser
 * text is the only diagnostic the user can report — dropping it (as this module
 * did before the web UI switched over) made the shipped guidance strictly less
 * useful than web-client's own copy.
 */
export function classifyMicError(name: string | undefined, message?: string): string {
  switch (name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return 'Microphone access denied. Allow mic for this site in browser settings, then click Connect again.';
    case 'NotReadableError':
    case 'AbortError':
      return 'Microphone is in use by another app or tab (Zoom, Photo Booth, another tab, or a prior session). Close it, then click Connect again.';
    case 'NotFoundError':
    case 'OverconstrainedError':
      return 'No microphone found. Connect an input device and select it as the default in your OS sound settings, then Connect.';
    default:
      return (
        'Microphone error (' +
        (name || 'unknown') +
        '): ' +
        (message || 'could not start capture') +
        '. Click Connect to retry.'
      );
  }
}

// ─── Transport ────────────────────────────────────────────────

export type VoiceStatus = 'idle' | 'connecting' | 'live' | 'error' | 'closed';

/**
 * Why the close event carries the raw code/reason: the surface — not the
 * transport — owns reconnect policy (attempt caps, retry delay, and the
 * troubleshooting copy that names GEMINI_API_KEY and the Discord link). It
 * cannot make that decision without distinguishing a clean server goodbye
 * (code 4000) or a user-initiated disconnect from an unexpected drop, so the
 * code and reason are handed through verbatim rather than flattened into
 * `detail`.
 */
export interface VoiceCloseInfo {
  code: number;
  reason: string;
}

export interface VoiceTransportEvents {
  /**
   * Connection lifecycle. `detail` is a human string for the status line.
   * `close` is present only for `status === 'closed'`, carrying the WS close
   * code and reason so the surface can apply its own reconnect policy.
   */
  onStatus?(status: VoiceStatus, detail?: string, close?: VoiceCloseInfo): void;
  /**
   * Optional trace sink for the surface's debug panel and its downloadable
   * debug dump. `kind` mirrors web-client's dbg() channels ('audio' | 'event' |
   * 'err' | 'warn' | undefined). Off unless the surface supplies it.
   */
  onDebug?(msg: string, kind?: string): void;
  /** Server transcript frame. `partial=false` means finalized. */
  onTranscript?(role: string, text: string, partial: boolean): void;
  /** Assistant turn ended normally. Playback is NOT flushed — the final audio
   *  is allowed to drain. Surfaces use this to reset per-turn UI state. */
  onTurnEnd?(): void;
  /** Assistant turn was interrupted (barge-in): scheduled playback has just been
   *  flushed so the user isn't spoken over. Surfaces reset per-turn UI state. */
  onInterrupted?(): void;
  /** Negotiated audio rates from `session.config`. */
  onSessionConfig?(inputRate: number, outputRate: number): void;
  /** Any non-audio protocol frame (image/video/chat/gui/etc). Surface renders it. */
  onProtocolMessage?(msg: any): void;
  /** Mic failed to start. `friendly` is from classifyMicError. */
  onMicError?(name: string, message: string, friendly: string): void;
  /** Optional live-audio AnalyserNode for avatar viz (playback path). */
  onAnalyser?(node: AnalyserNode): void;
  /** Byte counters, ~2×/s, for a stats panel. */
  onStats?(stats: { bytesSent: number; bytesRecv: number }): void;
}

export interface VoiceTransportOptions extends VoiceTransportEvents {
  /** Mic capture buffer size (ScriptProcessor). Default 2048, matching web-client. */
  captureBuf?: number;
  /** Default input rate until `session.config` overrides. Default 16000. */
  inputRate?: number;
  /** Default output rate until `session.config` overrides. Default 24000. */
  outputRate?: number;
  /** Playback speed multiplier. Default 1.0. */
  playbackRate?: number;
}

/**
 * One live voice session against a bodhi/Gemini WS endpoint. The `url` comes
 * from the tier resolver (voice-connect-resolver) — the transport itself is
 * tier-agnostic: local ws://localhost:9900, LAN, relay, or cloud all look the
 * same here.
 */
export class VoiceTransport {
  private ev: VoiceTransportEvents;
  private captureBuf: number;
  private inputRate: number;
  private outputRate: number;
  private playbackRate: number;

  private ws: WebSocket | null = null;
  private audioCtx: AudioContext | null = null;
  private micStream: MediaStream | null = null;
  private processor: ScriptProcessorNode | null = null;
  private analyserNode: AnalyserNode | null = null;
  private activeSources: AudioBufferSourceNode[] = [];
  private nextPlayTime = 0;

  private bytesSent = 0;
  private bytesRecv = 0;
  private statsTimer: ReturnType<typeof setInterval> | null = null;

  // Debug-panel counters. They exist to bound the trace output (first N only),
  // not as protocol state — the surface reads byte totals from onStats.
  private audioChunksRecv = 0;
  private micSendCount = 0;
  private playChunkCount = 0;

  constructor(opts: VoiceTransportOptions = {}) {
    this.ev = opts;
    this.captureBuf = opts.captureBuf ?? 2048;
    this.inputRate = opts.inputRate ?? 16000;
    this.outputRate = opts.outputRate ?? 24000;
    this.playbackRate = opts.playbackRate ?? 1.0;
  }

  get connected(): boolean {
    return !!this.ws && this.ws.readyState === WebSocket.OPEN;
  }

  /**
   * Open the session. Creates the AudioContext eagerly (call from a user
   * gesture so it isn't born suspended), connects the WS, and starts the mic
   * on open. Rejects only on synchronous setup failure; mic errors surface via
   * onMicError (the session stays up so the user can retry).
   */
  async connect(url: string): Promise<void> {
    if (!url) throw new Error('connect: empty url');
    this.status('connecting', 'Connecting…');

    // Create the AudioContext up front (ideally on a user gesture) so playback
    // and capture share one clock and it isn't born suspended.
    if (!this.audioCtx || this.audioCtx.state === 'closed') {
      this.audioCtx = new AudioContext();
    }
    if (this.audioCtx.state === 'suspended') {
      try {
        await this.audioCtx.resume();
      } catch {
        /* resumed lazily in playChunk if this races */
      }
    }

    const ws = new WebSocket(url);
    ws.binaryType = 'arraybuffer';
    this.ws = ws;

    ws.onopen = async () => {
      this.status('live', 'Starting mic…');
      try {
        await this.startMic();
        this.status('live', 'Live — speak now');
        this.statsTimer = setInterval(() => {
          this.ev.onStats?.({ bytesSent: this.bytesSent, bytesRecv: this.bytesRecv });
        }, 500);
      } catch (err: any) {
        const name = err?.name ?? 'unknown';
        const friendly = classifyMicError(err?.name, err?.message);
        this.debug('Mic error: ' + (err?.name ? err.name + ': ' : '') + err?.message, 'err');
        this.status('error', 'Mic error');
        this.ev.onMicError?.(name, err?.message ?? '', friendly);
        // Prevent an auto-reconnect loop on a hard mic failure.
        try {
          ws.close();
        } catch {
          /* already closing */
        }
      }
    };

    ws.onmessage = (event: MessageEvent) => this.onMessage(event);

    ws.onerror = () => {
      this.debug('WS error', 'err');
      this.status('error', 'Connection failed');
    };

    ws.onclose = (event: CloseEvent) => {
      // A server-initiated close must tear down the whole audio graph, not just
      // the stats timer — otherwise mic capture keeps running and stale audio
      // state lingers. Mirrors web-client's doCleanup() on ws.onclose.
      const code = event?.code ?? 0;
      const reason = event?.reason ?? '';
      this.debug('WS closed: code=' + code + ' reason=' + reason);
      this.teardownAudio();
      this.status('closed', 'Disconnected', { code, reason });
    };
  }

  /** Tear down mic + WS + playback + audio graph. Idempotent. */
  disconnect(): void {
    if (this.ws) {
      try {
        this.ws.close();
      } catch {
        /* already closed */
      }
      this.ws = null;
    }
    this.teardownAudio();
  }

  /** Alias for disconnect — teardown already closes the AudioContext. */
  close(): void {
    this.disconnect();
  }

  /**
   * Stop capture, flush playback, and close the audio graph. Closes the
   * AudioContext IMMEDIATELY (not on a delayed timeout): a deferred close can
   * race a reconnect and kill the freshly-created context. Faithful to
   * web-client's doCleanup(). Idempotent.
   */
  private teardownAudio(): void {
    this.stopMic();
    this.stopStats();
    this.flushPlayback();
    if (this.audioCtx && this.audioCtx.state !== 'closed') {
      try {
        this.audioCtx.close();
      } catch {
        /* ignore */
      }
    }
    this.audioCtx = null;
    this.analyserNode = null; // recreated against the next ctx in playChunk
  }

  // ─── mic capture ────────────────────────────────────────────

  private async startMic(): Promise<void> {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      // Condition and copy are verbatim from web-client. Deliberately keyed on
      // location rather than isSecureContext: the two agree on every surface we
      // ship today, but this is the string a user reads when the mic will not
      // start, and the shipped wording is the one to preserve.
      const isLocalhost =
        window.location.hostname === 'localhost' ||
        window.location.hostname === '127.0.0.1' ||
        window.location.hostname === '[::1]';
      const isHttps = window.location.protocol === 'https:';

      if (!isLocalhost && !isHttps) {
        throw new Error(
          'Microphone access requires HTTPS. Please access this page via HTTPS (https://your-domain.com) or use localhost. Modern browsers block getUserMedia on HTTP for security.',
        );
      } else {
        throw new Error(
          'Microphone access is not available in this browser. Please use a modern browser that supports getUserMedia.',
        );
      }
    }

    this.micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });

    if (!this.audioCtx || this.audioCtx.state === 'closed') {
      this.audioCtx = new AudioContext();
    }
    if (this.audioCtx.state === 'suspended') {
      await this.audioCtx.resume();
    }

    const ctx = this.audioCtx;
    const source = ctx.createMediaStreamSource(this.micStream);
    const processor = ctx.createScriptProcessor(this.captureBuf, 1, 1);
    this.processor = processor;

    processor.onaudioprocess = (e: AudioProcessingEvent) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
      const raw = e.inputBuffer.getChannelData(0);
      const down = downsample(raw, ctx.sampleRate, this.inputRate);
      const pcm = float32ToInt16(down);
      // pcm.buffer is a freshly-allocated ArrayBuffer (float32ToInt16 does
      // `new Int16Array(len)`), so the ArrayBufferLike→ArrayBuffer cast is safe.
      this.ws.send(pcm.buffer as ArrayBuffer);
      this.bytesSent += pcm.buffer.byteLength;
      this.micSendCount++;
      if (this.micSendCount <= 3) {
        this.debug(
          'Sent mic #' + this.micSendCount + ': ' + pcm.buffer.byteLength + 'B (' +
            down.length + ' samples @ ' + this.inputRate + 'Hz)',
          'audio',
        );
      }
    };

    source.connect(processor);
    // ScriptProcessor only fires while connected to the graph; route it through
    // a muted gain so it runs without leaking mic audio to the speakers.
    const silence = ctx.createGain();
    silence.gain.value = 0;
    processor.connect(silence);
    silence.connect(ctx.destination);
  }

  private stopMic(): void {
    if (this.processor) {
      try {
        this.processor.disconnect();
      } catch {
        /* ignore */
      }
      this.processor = null;
    }
    if (this.micStream) {
      this.micStream.getTracks().forEach((t) => t.stop());
      this.micStream = null;
    }
    // Don't close audioCtx here — playback may still be draining.
  }

  // ─── WS message routing ─────────────────────────────────────

  private onMessage(event: MessageEvent): void {
    if (event.data instanceof ArrayBuffer) {
      this.bytesRecv += event.data.byteLength;
      this.audioChunksRecv++;
      // First few only: the debug panel wants proof audio is arriving, not a
      // line per 20ms chunk. Matches web-client's cap.
      if (this.audioChunksRecv <= 5) {
        this.debug('Recv audio #' + this.audioChunksRecv + ': ' + event.data.byteLength + 'B', 'audio');
      }
      this.playChunk(event.data);
      return;
    }
    let msg: any;
    try {
      msg = JSON.parse(event.data);
    } catch {
      this.debug('Bad JSON text frame', 'warn');
      return; // non-JSON text frame — ignore
    }
    this.debug('Recv: ' + JSON.stringify(msg), 'event');

    if (msg?.type === 'session.config' && msg.audioFormat) {
      this.inputRate = msg.audioFormat.inputSampleRate ?? this.inputRate;
      this.outputRate = msg.audioFormat.outputSampleRate ?? this.outputRate;
      this.ev.onSessionConfig?.(this.inputRate, this.outputRate);
    } else if (msg?.type === 'transcript') {
      this.ev.onTranscript?.(msg.role, msg.text, msg.partial !== false);
    } else if (msg?.type === 'turn.end') {
      // Normal end of the assistant's turn — do NOT flush; the final scheduled
      // audio must be allowed to drain, or the last words get cut off. Only the
      // surface reacts (per-turn UI reset).
      this.ev.onTurnEnd?.();
    } else if (msg?.type === 'turn.interrupted') {
      // Barge-in: the user started speaking over the assistant. Stop all
      // scheduled playback immediately so it doesn't talk over them.
      this.flushPlayback();
      this.ev.onInterrupted?.();
    }

    // Always forward the raw frame — surfaces render image/video/gui/chat/etc.
    this.ev.onProtocolMessage?.(msg);
  }

  // ─── gapless playback ───────────────────────────────────────

  private playChunk(arrayBuf: ArrayBuffer): void {
    if (!this.audioCtx || this.audioCtx.state === 'closed') {
      try {
        this.audioCtx = new AudioContext();
      } catch {
        return;
      }
    }
    const ctx = this.audioCtx;
    if (ctx.state === 'suspended') ctx.resume();

    const f32 = int16ToFloat32(arrayBuf);
    if (f32.length === 0) return;

    try {
      const audioBuf = ctx.createBuffer(1, f32.length, this.outputRate);
      audioBuf.getChannelData(0).set(f32);

      const src = ctx.createBufferSource();
      src.buffer = audioBuf;
      src.playbackRate.value = this.playbackRate;

      if (!this.analyserNode) {
        this.analyserNode = ctx.createAnalyser();
        this.analyserNode.fftSize = 256;
        this.analyserNode.connect(ctx.destination);
        this.ev.onAnalyser?.(this.analyserNode);
      }
      src.connect(this.analyserNode);

      const now = ctx.currentTime;
      if (this.nextPlayTime < now) {
        this.nextPlayTime = now + 0.05;
      }
      src.start(this.nextPlayTime);
      this.nextPlayTime += audioBuf.duration / this.playbackRate;
      this.activeSources.push(src);
      src.onended = () => {
        const idx = this.activeSources.indexOf(src);
        if (idx >= 0) this.activeSources.splice(idx, 1);
      };
      this.playChunkCount++;
      if (this.playChunkCount <= 5) {
        this.debug(
          'Played chunk #' + this.playChunkCount + ': ' + f32.length +
            ' samples, scheduled at ' + this.nextPlayTime.toFixed(3) +
            's (ctx.state=' + ctx.state + ')',
          'audio',
        );
      }
    } catch (err: any) {
      /* transient scheduling error — drop this chunk */
      this.debug('playChunk error: ' + (err?.message ?? err), 'err');
    }
  }

  /** Stop and drop all scheduled playback (barge-in / disconnect). */
  private flushPlayback(): void {
    for (const s of this.activeSources) {
      try {
        s.stop();
      } catch {
        /* already stopped */
      }
    }
    this.activeSources = [];
    this.nextPlayTime = 0;
  }

  // ─── helpers ────────────────────────────────────────────────

  private status(s: VoiceStatus, detail?: string, close?: VoiceCloseInfo): void {
    this.ev.onStatus?.(s, detail, close);
  }

  private debug(msg: string, kind?: string): void {
    this.ev.onDebug?.(msg, kind);
  }

  private stopStats(): void {
    if (this.statsTimer) {
      clearInterval(this.statsTimer);
      this.statsTimer = null;
    }
  }
}
