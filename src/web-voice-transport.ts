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
 *   - the Sutando webUI (src/web-client.ts — loads the IIFE artifact served
 *     at /web-voice-transport.js and instantiates SutandoVoice.VoiceTransport)
 *   - the cinny desktop client (vendored VERBATIM — any change lands here
 *     first and is re-copied; see the desktop repo's vendor gate)
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
 *   turn.end barge-in (flush playback)   reconnect policy
 *   connect timeout + failure decoding   error-card routing / rescue flow
 *   `agent.state` (L3) client handling
 *   mute / deafen call-control gates
 *
 * The seam is the event callbacks: the transport plays audio itself and emits
 * every non-audio protocol frame to `onProtocolMessage` (plus typed shortcuts
 * for the common ones) so each surface renders in its own framework.
 *
 * Reliability contract (design 1e; impl plan WS1 Step 18, amendments
 * R10/R12/S6/W5/X6/Z6):
 *   - every `connect()` is one ATTEMPT with a generation token; stale socket
 *     callbacks and stale post-`await` continuations are silently discarded;
 *   - attempt concluded ⇒ generation invalidated: EVERY path that concludes
 *     an attempt (timeout, mic failure, pre-open error, every close branch,
 *     upstream-failed, disconnect(), a superseding connect()) bumps the
 *     generation, so a continuation parked in an `await` (getUserMedia
 *     prompt, AudioContext.resume) can never resume a dead attempt;
 *   - a connect attempt that does not reach `onopen` within CONNECT_TIMEOUT_MS
 *     fails with a latched terminal `error`;
 *   - terminal failures LATCH: the close they trigger never overwrites the
 *     `error` status with `closed`;
 *   - `agent.state` v1 frames drive L3 handling — `upstream:'failed'` is a
 *     terminal CLIENT transition (the server deliberately stays reachable);
 *     a server that sends no frame within the legacy window simply behaves
 *     exactly as before the protocol existed;
 *   - every failed attempt is classified into the exported
 *     `VoiceConnectFailure` union exactly once via `onConnectFailure`;
 *   - teardown is awaitable (T8): `disconnect()`/`close()` return a promise
 *     that resolves once the underlying socket's real close handshake
 *     completes (bounded), so lease owners can await teardown before
 *     releasing exclusivity — the synchronous `closed` status still fires
 *     first, exactly as before;
 *   - EVERY attempt conclusion exposes that same completion via
 *     `closeSettled()` (T8 generalized): self-initiated terminal closes
 *     (connect timeout, mic failure, pre-open socket error, upstream-failed)
 *     track their socket's real close handshake exactly like disconnect(),
 *     and close-derived conclusions (statuses decoded from a WS close frame)
 *     are already settled when they emit. After any terminal status /
 *     onConnectFailure, consumers releasing single-client resources must
 *     await closeSettled() before releasing, or the server may still count
 *     the departing client (spurious 4409 on the next attempt).
 *
 * The DSP functions are pure and exported standalone so they unit-test in Node
 * (see tests/web-voice-transport.test.ts). The class is drivable from node:test
 * through the `wsFactory` seam; browser-only paths (AudioContext, getUserMedia)
 * are exercised by the surfaces + the happy-path spike.
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

/** Machine-readable mic-error class (impl plan Step 15 — Step 19's routing
 *  needs a code, not prose). Same DOMException partition as classifyMicError. */
export type MicErrorCode = 'permission' | 'device' | 'unknown';

export function classifyMicErrorCode(name: string | undefined): MicErrorCode {
  switch (name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return 'permission';
    case 'NotReadableError':
    case 'AbortError':
    case 'NotFoundError':
    case 'OverconstrainedError':
      return 'device';
    default:
      return 'unknown';
  }
}

// ─── `agent.state` v1 (design 1a′) ────────────────────────────
// Duplicated from src/voice-agent-state.ts on purpose: this module is vendored
// standalone into other repos and must not import server-side modules. Pinned
// to the v1 frame — the schema is WIRE CONTRACT v1; if the emitter ever bumps
// `v`, this copy (and the legacy fallback below) must be revisited together.

/** L3 upstream states (design readiness model). */
export type AgentUpstreamState = 'live' | 'idle' | 'connecting' | 'backoff' | 'failed';

/** Terminal-failure classes carried by `agent.state` frames. */
export type AgentStateCategory = 'auth' | 'quota' | 'network' | 'other';

/** `agent.state` v1 frame — design 1a′, verbatim schema. */
export interface AgentStateV1 {
  type: 'agent.state';
  v: 1;
  initialized: boolean;
  upstream: AgentUpstreamState;
  reason?: string;
  category?: AgentStateCategory;
  clientAttached: boolean;
  credentialSource?: 'managed' | 'byok';
  credentialGeneration?: string;
  launchdContract?: 1;
}

// ─── Connect-failure union (impl plan Step 18; R12 + W5 + X6 + Z6) ──────────

/**
 * Application close code the server uses to reject a second real client while
 * one is attached (amendment W5). All surfaces decode it identically.
 */
export const CLOSE_CODE_CLIENT_BUSY = 4409;

/**
 * Application close code the server sends to the INCUMBENT client when a
 * user-confirmed takeover moves the call to another surface (amendment W5).
 * Decoded as its own terminal status (`'superseded'`), not a connect failure.
 */
export const CLOSE_CODE_SUPERSEDED_BY_TAKEOVER = 4410;

/**
 * Why a connect attempt failed, as one closed discriminated union (R12):
 *
 *   'timeout'        — no `onopen` within the connect timeout.
 *   'connect-error'  — browser-observable pre-open failure. Browsers expose
 *                      only a generic error + close 1006 here — no
 *                      ECONNREFUSED/TLS/DNS distinction (Z6), so a `refused`
 *                      refinement deliberately does NOT exist in this union;
 *                      only a native probe with an errno could supply one.
 *   'mic-permission' — getUserMedia permission denied.
 *   'mic-device'     — mic busy / missing / unreadable.
 *   'mic-other'      — unclassified mic failure (X6): remediation is Retry —
 *                      it must never be routed to credential repair.
 *   'agent-failed'   — `agent.state` reported `upstream:'failed'` (terminal:
 *                      bad key / quota / credits). Carries reason + category.
 *   'service-down'   — a SUPERVISOR-STATUS conclusion, produced by surfaces
 *                      that consult supervisor-status; the transport itself
 *                      never emits it (the browser cannot observe it — Z6).
 *   'client-busy'    — server rejected this client with close 4409 while
 *                      another real client is attached (W5). Surfaces show
 *                      "voice is in use elsewhere" with a take-over
 *                      affordance (`?takeover=1` on the next connect).
 */
export type VoiceConnectFailureKind =
  | 'timeout'
  | 'connect-error'
  | 'mic-permission'
  | 'mic-device'
  | 'mic-other'
  | 'agent-failed'
  | 'service-down'
  | 'client-busy';

export interface VoiceConnectFailure {
  kind: VoiceConnectFailureKind;
  /** Classified human-readable detail (safe to render). */
  detail: string;
  /** Actionable remediation hint (safe to render). */
  remediation: string;
  /** Stable failure code from `agent.state` (kind 'agent-failed' only). */
  reason?: string;
  /** Failure class from `agent.state` (kind 'agent-failed' only). */
  category?: AgentStateCategory;
  /** Raw WS close info when the failure was decoded from a close frame. */
  close?: VoiceCloseInfo;
}

/** Default remediation hint per failure kind — one shared vocabulary so every
 *  surface (rail, webUI page, flow verifier) says the same thing. */
export const VOICE_FAILURE_REMEDIATION: Record<VoiceConnectFailureKind, string> = {
  timeout: 'Retry; if it keeps timing out, open voice setup to check the voice service.',
  'connect-error': 'Check that the voice service is running, then retry.',
  'mic-permission': 'Allow microphone access for this app in system/browser settings, then retry.',
  'mic-device': 'Close other apps using the microphone or connect an input device, then retry.',
  // X6: unclassified mic failures get Retry — never credential repair.
  'mic-other': 'Retry.',
  'agent-failed': 'Open voice setup to check the voice service.',
  'service-down': 'Restart the voice service from voice setup.',
  'client-busy': 'Voice is in use on another surface. Close it there, or take over the call here.',
};

/**
 * Classify an `agent.state` terminal failure into detail + remediation. The
 * server deliberately keeps serving while upstream is failed, so this text is
 * what the user sees instead of an eternal spinner.
 */
export function describeAgentFailure(
  reason?: string,
  category?: AgentStateCategory,
): { detail: string; remediation: string } {
  const code = reason ? ' (' + reason + ')' : '';
  switch (category) {
    case 'auth':
      return {
        detail: 'Voice agent rejected by Gemini: credential problem' + code + '.',
        remediation: 'Fix your Gemini key in voice setup, then retry.',
      };
    case 'quota':
      return {
        detail: 'Voice agent out of Gemini quota or credits' + code + '.',
        remediation: 'Check your Gemini plan/quota, then retry.',
      };
    case 'network':
      return {
        detail: 'Voice agent cannot reach Gemini' + code + '.',
        remediation: 'Check your network connection, then retry.',
      };
    default:
      return {
        detail: 'Voice agent upstream failed' + code + '.',
        remediation: VOICE_FAILURE_REMEDIATION['agent-failed'],
      };
  }
}

// ─── Transport ────────────────────────────────────────────────

/**
 * Connection lifecycle status. `'superseded'` is the terminal state of an
 * incumbent whose call was moved by a user-confirmed takeover (close 4410) —
 * distinct from `'closed'` so surfaces can render "call moved elsewhere"
 * instead of a generic disconnect (and must not auto-reconnect into a fight).
 */
export type VoiceStatus = 'idle' | 'connecting' | 'live' | 'error' | 'closed' | 'superseded';

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
   * `close` is present when the transition was decoded from a WS close frame:
   * every `'closed'` decoded from a WS close frame carries it, as do the
   * close-derived terminal states (`'error'` on 4409/pre-open closes,
   * `'superseded'` on 4410). A user-initiated `disconnect()` emits `'closed'`
   * WITHOUT close info — no WS close frame is involved in that transition
   * (the promise `disconnect()` returns tracks the socket's real close
   * handshake instead; the synchronous `closed` always fires first).
   */
  onStatus?(status: VoiceStatus, detail?: string, close?: VoiceCloseInfo): void;
  /**
   * Exactly-once-per-attempt classified connect failure (impl plan Step 18).
   * Fires alongside the latched `error` status; surfaces route on `kind`
   * (mic-permission → permission flow, agent-failed/timeout → voice setup,
   * client-busy → take-over affordance, …).
   *
   * Lease-release contract (P1): after this fires (or any terminal status),
   * `closeSettled()` settles once the underlying socket's close handshake is
   * done — already resolved for close-derived conclusions, tracked (bounded)
   * for self-initiated terminal closes. Consumers releasing single-client
   * resources (e.g. a voice lease) must await it before releasing, or the
   * server may still count this client and reject the next attempt with 4409.
   */
  onConnectFailure?(failure: VoiceConnectFailure): void;
  /**
   * Every `agent.state` v1 frame received on the live connection, verbatim
   * (design 1a′). The transport already applies the client rules (progress
   * detail for connecting/backoff, terminal handling for failed); surfaces
   * use this for richer readiness UI. Never fires on legacy servers.
   */
  onAgentState?(state: AgentStateV1): void;
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

/** Default connect timeout (design 1e): if the WS has not reached `onopen`
 *  within this window, the attempt fails with a latched terminal error. */
export const CONNECT_TIMEOUT_MS = 6000;

/** Legacy-server detection window (design 1a′ client rules): no `agent.state`
 *  frame within this window after open ⇒ legacy server, behave exactly as
 *  before the protocol existed. */
export const AGENT_STATE_LEGACY_MS = 3000;

/** Bound on the awaited close handshake in `disconnect()` (amendment T8): if
 *  the socket's real `close` event never arrives within this window, the
 *  returned teardown promise resolves anyway — a wedged handshake must not
 *  hang lease release. */
export const DISCONNECT_CLOSE_TIMEOUT_MS = 1500;

export interface VoiceTransportOptions extends VoiceTransportEvents {
  /** Mic capture buffer size (ScriptProcessor). Default 2048, matching web-client. */
  captureBuf?: number;
  /** Default input rate until `session.config` overrides. Default 16000. */
  inputRate?: number;
  /** Default output rate until `session.config` overrides. Default 24000. */
  outputRate?: number;
  /** Playback speed multiplier. Default 1.0. */
  playbackRate?: number;
  /** Connect timeout override (tests). Default CONNECT_TIMEOUT_MS. */
  connectTimeoutMs?: number;
  /** Legacy-detection window override (tests). Default AGENT_STATE_LEGACY_MS. */
  agentStateLegacyMs?: number;
  /** Bound on disconnect()'s awaited close handshake (tests). Default
   *  DISCONNECT_CLOSE_TIMEOUT_MS. */
  disconnectCloseTimeoutMs?: number;
  /**
   * Socket factory seam so the state machine is drivable from node:test
   * without a browser. Default `new WebSocket(url)`. Fakes provide the
   * `onopen/onmessage/onerror/onclose` + `send/close/readyState` surface and
   * cast to WebSocket.
   */
  wsFactory?: (url: string) => WebSocket;
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
  private connectTimeoutMs: number;
  private agentStateLegacyMs: number;
  private disconnectCloseTimeoutMs: number;
  private wsFactory: (url: string) => WebSocket;

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

  // Call controls (owner 2026-07-15; reconciled from the cinny surface —
  // impl plan Step 15): mute self = stop sending mic to the agent; deafen =
  // stop playing the agent's audio. Both are additive gates on the existing
  // capture/playback paths — they don't touch the transport lifecycle.
  private micMuted = false;
  private deafened = false;

  // ── Attempt state (impl plan Step 18) ──
  // One `connect()` = one attempt. The generation token invalidates every
  // stale socket callback and stale post-`await` continuation (R10) — and the
  // invariant is uniform: attempt concluded ⇒ generation invalidated, i.e.
  // every concluding path bumps `attemptGen` (see handleClose). The terminal
  // latch keeps a failed attempt's `error` from being overwritten by
  // its own self-inflicted close; `attemptActive` guarantees exactly one
  // final status per attempt (S6); `failureEmitted` guarantees exactly one
  // onConnectFailure per attempt.
  private attemptGen = 0;
  private terminal = false;
  private attemptActive = false;
  private opened = false;
  private failureEmitted = false;
  private connectTimer: ReturnType<typeof setTimeout> | null = null;
  private legacyTimer: ReturnType<typeof setTimeout> | null = null;
  private agentStateSeen = false;
  private legacyServer = false;
  private lastUpstream: AgentUpstreamState | null = null;
  /** Current attempt-conclusion close completion — see closeSettled(). Starts
   *  resolved: no socket ever existed. */
  private closeCompletion: Promise<void> = Promise.resolve();

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
    this.connectTimeoutMs = opts.connectTimeoutMs ?? CONNECT_TIMEOUT_MS;
    this.agentStateLegacyMs = opts.agentStateLegacyMs ?? AGENT_STATE_LEGACY_MS;
    this.disconnectCloseTimeoutMs = opts.disconnectCloseTimeoutMs ?? DISCONNECT_CLOSE_TIMEOUT_MS;
    this.wsFactory = opts.wsFactory ?? ((url: string) => new WebSocket(url));
  }

  get connected(): boolean {
    return !!this.ws && this.ws.readyState === WebSocket.OPEN;
  }

  /** What the server's `agent.state` support turned out to be for the current
   *  attempt: 'v1' once a frame arrived, 'legacy' once the detection window
   *  elapsed with none, 'unknown' before either. */
  get agentStateSupport(): 'unknown' | 'v1' | 'legacy' {
    if (this.agentStateSeen) return 'v1';
    if (this.legacyServer) return 'legacy';
    return 'unknown';
  }

  /**
   * Open the session. Creates the AudioContext eagerly (call from a user
   * gesture so it isn't born suspended), connects the WS, and starts the mic
   * on open.
   *
   * FIRE-AND-FORGET CONTRACT: the returned promise resolves when the attempt
   * is *initiated* (or rejects only on synchronous setup failure such as an
   * empty url). Every asynchronous outcome — open, timeout, mic failure,
   * agent failure, close — is reported via the callbacks, never the promise.
   */
  async connect(url: string): Promise<void> {
    if (!url) throw new Error('connect: empty url');

    // New attempt: invalidate everything from the previous one (its socket
    // callbacks are generation-fenced below) and reset the attempt state.
    const gen = ++this.attemptGen;
    this.terminal = false;
    this.attemptActive = true;
    this.opened = false;
    this.failureEmitted = false;
    this.agentStateSeen = false;
    this.legacyServer = false;
    this.lastUpstream = null;
    this.clearConnectTimer();
    this.clearLegacyTimer();
    if (this.ws) {
      // A Retry on the same transport must not stack a second socket or
      // audio graph on top of a leftover one.
      try {
        this.ws.close();
      } catch {
        /* already closed */
      }
      this.ws = null;
    }
    this.stopMic();
    this.stopStats();
    // A direct connect() over a still-live session (no disconnect() between)
    // must not let the old session's scheduled playback keep speaking into
    // the new attempt — and nextPlayTime must restart from the new clock
    // instead of continuing the old session's schedule.
    this.flushPlayback();

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
      // R10: re-check the generation after EVERY await — a disconnect() (or a
      // newer connect()) during resume() must not create a replacement socket.
      if (gen !== this.attemptGen) return;
    }

    const ws = this.wsFactory(url);
    ws.binaryType = 'arraybuffer';
    this.ws = ws;

    // Design 1e: a connection that never reaches onopen must fail visibly.
    this.connectTimer = setTimeout(() => this.onConnectTimeout(gen), this.connectTimeoutMs);

    ws.onopen = () => {
      void this.handleOpen(gen, ws);
    };
    ws.onmessage = (event: MessageEvent) => {
      if (gen !== this.attemptGen) return; // stale socket — discard silently
      this.onMessage(event);
    };
    ws.onerror = () => {
      if (gen !== this.attemptGen) return;
      this.handleSocketError();
    };
    ws.onclose = (event: CloseEvent) => {
      if (gen !== this.attemptGen) return; // a stale close can't clobber a newer attempt
      this.handleClose(event);
    };
  }

  /** Mute/unmute the local mic — stop/resume sending audio to the agent. Also
   *  flips the mic track so the OS mic indicator reflects the mute. */
  setMicMuted(muted: boolean): void {
    this.micMuted = muted;
    // Flip the track (typically one) so the OS mic indicator reflects the mute.
    const track = this.micStream?.getAudioTracks()[0];
    if (track) track.enabled = !muted;
  }

  /** Deafen/undeafen — stop/resume playing the agent's audio. Deafening flushes
   *  any in-flight playback so it stops immediately. */
  setDeafened(deafened: boolean): void {
    this.deafened = deafened;
    if (deafened) this.flushPlayback();
  }

  /** Live playback-speed control (the `speech_speed` protocol frame is
   *  interpreted by the surface, which applies it here). */
  setPlaybackRate(rate: number): void {
    this.playbackRate = rate;
  }

  /** Send a typed text input over the live session (the surface's text box
   *  during voice). Returns false when no socket is open. */
  sendTextInput(text: string): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    this.ws.send(JSON.stringify({ type: 'text_input', text }));
    return true;
  }

  /**
   * User-initiated teardown of mic + WS + playback + audio graph. Idempotent.
   *
   * Amendment S6: disconnect() invalidates the attempt FIRST — every in-flight
   * `await` continuation and socket callback goes stale — then synchronously
   * emits exactly ONE `closed` transition. It must not depend on the socket's
   * `onclose` (now generation-fenced, hence suppressed), or the UI could stick
   * in `connecting`/`live` forever. The terminal-ERROR cleanup path stays
   * separate: after a latched `error` (timeout / upstream-failed / mic),
   * disconnect() only cleans up and PRESERVES the latched error status.
   *
   * Amendment T8 (awaitable teardown): the RETURNED PROMISE resolves once the
   * underlying WebSocket's real close handshake completes — i.e. the socket's
   * own `close` event has fired — so lease owners can await teardown before
   * releasing exclusivity. The synchronous behavior above is unchanged and the
   * synchronous `closed` status always fires BEFORE the promise resolves. An
   * already-CLOSED socket resolves immediately; with no socket the current
   * attempt-conclusion completion is returned (immediately resolved unless a
   * terminal latch already closed a socket whose handshake is still in
   * flight — so `await disconnect()` always covers the last real teardown);
   * a wedged handshake is bounded by `disconnectCloseTimeoutMs` (default
   * DISCONNECT_CLOSE_TIMEOUT_MS) so teardown can never hang.
   */
  disconnect(): Promise<void> {
    this.attemptGen++;
    this.clearConnectTimer();
    this.clearLegacyTimer();
    const emitClosed = this.attemptActive && !this.terminal;
    this.attemptActive = false;
    const ws = this.ws;
    const completion = this.trackCloseCompletion(ws);
    if (ws) {
      try {
        ws.close();
      } catch {
        /* already closed */
      }
      this.ws = null;
    }
    this.teardownAudio();
    if (emitClosed) {
      this.status('closed', 'Disconnected');
    }
    return completion;
  }

  /** Alias for disconnect — teardown already closes the AudioContext. Returns
   *  the same awaitable close-handshake completion (T8). */
  close(): Promise<void> {
    return this.disconnect();
  }

  /**
   * Track the REAL close-handshake completion of a socket this side is about
   * to close (factored from disconnect(); T8, generalized by the P1 fix to
   * every self-initiated terminal close). The returned promise resolves once
   * the socket's own `close` event has fired; a wedged handshake is bounded
   * by `disconnectCloseTimeoutMs`. Must be called BEFORE `close()` and BEFORE
   * `this.ws` is cleared. The wsFactory seam guarantees only the
   * onopen/onmessage/onerror/onclose property surface (fakes provide no
   * addEventListener), so this wraps the current onclose handler: it is
   * connect()'s generation-fenced closure — already a guaranteed no-op once
   * the caller has bumped the generation — and close fires at most once.
   * The result is latched as the attempt-conclusion completion returned by
   * `closeSettled()`; an already-CLOSED socket latches an immediately
   * resolved completion, and with no socket the prior completion is kept
   * (nothing new to hand-shake).
   */
  private trackCloseCompletion(ws: WebSocket | null): Promise<void> {
    if (!ws) return this.closeCompletion;
    if (ws.readyState === WebSocket.CLOSED) {
      // Nothing to hand-shake: the socket's close already completed.
      this.closeCompletion = Promise.resolve();
      return this.closeCompletion;
    }
    const completion = new Promise<void>((resolve) => {
      let settled = false;
      let fallback: ReturnType<typeof setTimeout> | null = null;
      const settle = (): void => {
        if (settled) return;
        settled = true;
        if (fallback) clearTimeout(fallback);
        resolve();
      };
      fallback = setTimeout(settle, this.disconnectCloseTimeoutMs);
      const prevOnClose = ws.onclose;
      ws.onclose = (event: CloseEvent) => {
        try {
          if (prevOnClose) prevOnClose.call(ws, event);
        } finally {
          settle();
        }
      };
    });
    this.closeCompletion = completion;
    return completion;
  }

  /**
   * The CURRENT attempt-conclusion close completion (P1 lease-release
   * contract). After ANY terminal status / onConnectFailure, the returned
   * promise settles once the underlying socket's close handshake is done:
   * self-initiated terminal closes (connect timeout, mic failure, pre-open
   * socket error, upstream-failed) track their own socket's close exactly
   * like disconnect() does — the completion is latched BEFORE the terminal
   * status/failure emits, so it can be read from inside the callbacks —
   * while close-derived conclusions ('closed'/'error'/'superseded' decoded
   * from a WS close frame) are already settled when they emit, because the
   * socket is already closed. Resolved immediately when no socket ever
   * existed; bounded by `disconnectCloseTimeoutMs`, so it can never hang.
   * Consumers releasing single-client resources (e.g. a voice lease) MUST
   * await this before releasing, or the server may still count the departing
   * client and reject the next attempt with 4409.
   */
  closeSettled(): Promise<void> {
    return this.closeCompletion;
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

  // ─── attempt outcome handling (Step 18) ─────────────────────

  private clearConnectTimer(): void {
    if (this.connectTimer) {
      clearTimeout(this.connectTimer);
      this.connectTimer = null;
    }
  }

  private clearLegacyTimer(): void {
    if (this.legacyTimer) {
      clearTimeout(this.legacyTimer);
      this.legacyTimer = null;
    }
  }

  /** Exactly one classified failure per attempt. */
  private emitFailure(failure: VoiceConnectFailure): void {
    if (this.failureEmitted) return;
    this.failureEmitted = true;
    this.ev.onConnectFailure?.(failure);
  }

  private onConnectTimeout(gen: number): void {
    this.connectTimer = null;
    if (gen !== this.attemptGen || this.terminal) return;
    const detail = 'Connection timed out';
    this.debug('Connect timeout after ' + this.connectTimeoutMs + 'ms', 'err');
    // Attempt concluded ⇒ generation invalidated: the bump fences out the
    // self-inflicted onclose and any stale continuation of this attempt; the
    // terminal latch (set BEFORE closing) stays as the second line of defense.
    this.attemptGen++;
    this.terminal = true;
    this.attemptActive = false;
    this.teardownAudio();
    // P1: latch the close-handshake completion BEFORE the terminal status/
    // failure emit, so closeSettled() read from inside the callbacks is
    // already THIS conclusion's completion (a lease released before the
    // handshake finishes leaves the server counting the old client →
    // spurious 4409 on the next attempt).
    const ws = this.ws;
    this.trackCloseCompletion(ws);
    this.status('error', detail);
    this.emitFailure({
      kind: 'timeout',
      detail,
      remediation: VOICE_FAILURE_REMEDIATION.timeout,
    });
    if (ws) {
      try {
        ws.close();
      } catch {
        /* already closed */
      }
      this.ws = null;
    }
  }

  private async handleOpen(gen: number, ws: WebSocket): Promise<void> {
    if (gen !== this.attemptGen) return;
    this.opened = true;
    this.clearConnectTimer();
    this.armLegacyTimer(gen);
    this.status('live', 'Starting mic…');
    try {
      await this.startMic(gen);
      // R10: a slow permission prompt can outlive the attempt it belonged to.
      // startMic() itself stopped any stale capture under its own generation
      // checks — this stale branch must NOT touch instance state (this.micStream
      // may already belong to a newer attempt).
      if (gen !== this.attemptGen) return;
      this.status('live', 'Live — speak now');
      this.statsTimer = setInterval(() => {
        this.ev.onStats?.({ bytesSent: this.bytesSent, bytesRecv: this.bytesRecv });
      }, 500);
    } catch (err: any) {
      if (gen !== this.attemptGen) return;
      const name = err?.name ?? 'unknown';
      const friendly = classifyMicError(err?.name, err?.message);
      const code = classifyMicErrorCode(err?.name);
      const kind: VoiceConnectFailureKind =
        code === 'permission' ? 'mic-permission' : code === 'device' ? 'mic-device' : 'mic-other';
      this.debug('Mic error: ' + (err?.name ? err.name + ': ' : '') + err?.message, 'err');
      // Attempt concluded ⇒ generation invalidated: the bump fences out the
      // self-inflicted onclose below and any continuation of this attempt
      // still parked in an await; the terminal latch keeps the error from
      // being overwritten, and a hard mic failure must not auto-reconnect.
      // That same fencing means no trailing handleClose will clean up for
      // this attempt — so tear the audio graph down HERE (startMic can have
      // captured a stream before throwing, e.g. a resume() failure after
      // getUserMedia was already granted).
      this.attemptGen++;
      this.terminal = true;
      this.attemptActive = false;
      this.clearLegacyTimer();
      this.teardownAudio();
      // P1: latch the self-inflicted close-handshake completion before any
      // callback emits (see onConnectTimeout).
      this.trackCloseCompletion(ws);
      this.status('error', 'Mic error');
      this.ev.onMicError?.(name, err?.message ?? '', friendly);
      this.emitFailure({ kind, detail: friendly, remediation: VOICE_FAILURE_REMEDIATION[kind] });
      if (this.ws === ws) this.ws = null;
      try {
        ws.close();
      } catch {
        /* already closing */
      }
    }
  }

  private handleSocketError(): void {
    this.debug('WS error', 'err');
    if (this.terminal) return;
    if (!this.opened) {
      // Pre-open failure. The browser exposes no errno/TLS/DNS detail here
      // (Z6) — just a generic error followed by close 1006 — so this is the
      // one browser-observable pre-open kind: 'connect-error'. Attempt
      // concluded ⇒ generation invalidated: the bump fences the trailing
      // 1006 close out at the closure; the terminal latch stays as the
      // second line of defense against a 'closed' overwrite.
      const detail = 'Connection failed';
      this.attemptGen++;
      this.terminal = true;
      this.attemptActive = false;
      this.clearConnectTimer();
      this.teardownAudio();
      // P1: latch the close-handshake completion before the terminal emits
      // (see onConnectTimeout). The browser's trailing 1006 close lands on
      // the wrapped handler and settles it.
      const ws = this.ws;
      this.trackCloseCompletion(ws);
      this.status('error', detail);
      this.emitFailure({
        kind: 'connect-error',
        detail,
        remediation: VOICE_FAILURE_REMEDIATION['connect-error'],
      });
      if (ws) {
        try {
          ws.close();
        } catch {
          /* already closed */
        }
        this.ws = null;
      }
      return;
    }
    // Mid-session socket error: not an attempt-terminal state — the close
    // that follows carries the code, and the surface owns reconnect policy.
    this.status('error', 'Connection failed');
  }

  private handleClose(event: CloseEvent): void {
    const code = event?.code ?? 0;
    const reason = event?.reason ?? '';
    this.debug('WS closed: code=' + code + ' reason=' + reason);
    this.clearConnectTimer();
    this.clearLegacyTimer();
    // Attempt concluded ⇒ generation invalidated. Every branch below
    // concludes the attempt, and the gen fence in connect()'s onclose closure
    // guarantees this method only ever runs for the CURRENT attempt — so one
    // unconditional bump covers them all. Without it, a continuation parked
    // in startMic's getUserMedia/resume() await would sail past its gen fence
    // once the prompt resolved: the dead attempt's status('live') would
    // overwrite the close-derived status emitted below, the just-granted mic
    // stream would stay captured, and the statsTimer would leak.
    this.attemptGen++;
    // P1: a close-derived conclusion needs no handshake tracking — the event
    // driving this method IS the socket's close, so the attempt-conclusion
    // completion is already settled by definition (closeSettled() resolves
    // immediately for every status emitted below). Self-initiated terminal
    // closes never reach here: their tracking wrapper replaced this socket's
    // onclose.
    this.closeCompletion = Promise.resolve();
    if (this.terminal) {
      // Latched terminal attempt (timeout / mic / pre-open error /
      // upstream-failed / client-busy / superseded): the self-inflicted or
      // trailing close still tears the audio graph down, but must NOT
      // overwrite the latched status (design 1e terminal-state latching).
      // Defense in depth: every latching path now bumps the generation
      // itself, fencing its trailing close out at the closure before it
      // reaches here — this branch stays as the net for any future
      // terminal-setter that forgets.
      this.teardownAudio();
      return;
    }
    this.teardownAudio();
    const close: VoiceCloseInfo = { code, reason };
    if (code === CLOSE_CODE_CLIENT_BUSY) {
      // W5: another real client is attached — surfaces render "voice is in
      // use elsewhere" with a take-over affordance.
      const detail = 'Voice is in use on another surface.';
      this.terminal = true;
      this.attemptActive = false;
      this.ws = null;
      this.status('error', detail, close);
      this.emitFailure({
        kind: 'client-busy',
        detail,
        remediation: VOICE_FAILURE_REMEDIATION['client-busy'],
        close,
      });
      return;
    }
    if (code === CLOSE_CODE_SUPERSEDED_BY_TAKEOVER) {
      // W5: a user-confirmed takeover moved the call to another surface.
      // Terminal state of its own — NOT a connect failure and NOT a plain
      // close (a surface auto-reconnecting here would fight the takeover).
      this.terminal = true;
      this.attemptActive = false;
      this.ws = null;
      this.status('superseded', 'Voice call moved to another surface', close);
      return;
    }
    if (!this.opened) {
      // Pre-open close without a usable close code (Z6: browsers surface
      // only 1006 here). Same terminal 'connect-error' as handleSocketError —
      // whichever of the two events arrives first latches, the other is
      // suppressed by the terminal check above.
      const detail = 'Connection failed';
      this.terminal = true;
      this.attemptActive = false;
      this.ws = null;
      this.status('error', detail, close);
      this.emitFailure({
        kind: 'connect-error',
        detail,
        remediation: VOICE_FAILURE_REMEDIATION['connect-error'],
        close,
      });
      return;
    }
    // Ordinary post-open close (server goodbye or unexpected drop). The
    // surface applies its own reconnect policy from the code/reason.
    this.attemptActive = false;
    this.ws = null;
    this.status('closed', 'Disconnected', close);
  }

  // ─── `agent.state` client handling (design 1a′) ─────────────

  private armLegacyTimer(gen: number): void {
    this.clearLegacyTimer();
    this.legacyTimer = setTimeout(() => {
      this.legacyTimer = null;
      if (gen !== this.attemptGen || this.agentStateSeen) return;
      // Legacy fallback: no frame within the window ⇒ older/external server.
      // Behave exactly as today — mic start and "Live" were already keyed on
      // onopen, so there is nothing to undo; just stop expecting frames.
      this.legacyServer = true;
      this.debug(
        'agent.state: no frame within ' + this.agentStateLegacyMs + 'ms — legacy server, no L3 signal',
        'warn',
      );
    }, this.agentStateLegacyMs);
  }

  private handleAgentState(frame: AgentStateV1): void {
    this.agentStateSeen = true;
    this.legacyServer = false;
    this.clearLegacyTimer();
    const prev = this.lastUpstream;
    this.lastUpstream = frame.upstream;
    this.ev.onAgentState?.(frame);
    if (this.terminal) return; // latched attempt — frames are informational only
    switch (frame.upstream) {
      case 'failed': {
        // Terminal CLIENT transition (design 1e): the server deliberately
        // stays reachable, so no close will arrive — the client itself must
        // invalidate the attempt, stop mic/stats/playback, close the socket,
        // latch the error, and suppress the self-inflicted onclose. Otherwise
        // the mic keeps streaming behind the error card and a Retry stacks a
        // second socket/audio graph.
        const { detail, remediation } = describeAgentFailure(frame.reason, frame.category);
        this.attemptGen++; // invalidate: no callback of this socket runs again
        this.terminal = true;
        this.attemptActive = false;
        this.clearConnectTimer();
        this.teardownAudio();
        const ws = this.ws;
        // P1: latch the self-initiated close-handshake completion before the
        // terminal status/failure emit (see onConnectTimeout).
        this.trackCloseCompletion(ws);
        this.ws = null;
        if (ws) {
          try {
            ws.close();
          } catch {
            /* already closed */
          }
        }
        this.status('error', detail);
        const failure: VoiceConnectFailure = { kind: 'agent-failed', detail, remediation };
        if (frame.reason !== undefined) failure.reason = frame.reason;
        if (frame.category !== undefined) failure.category = frame.category;
        this.emitFailure(failure);
        return;
      }
      case 'connecting':
      case 'backoff':
        // Progress, not an error: idle→connecting→live after connect is the
        // normal wake-up sequence.
        this.status('live', frame.upstream === 'connecting' ? 'Waking up…' : 'Reconnecting to the model…');
        return;
      case 'live':
        if (prev !== null && prev !== 'live') {
          this.status('live', 'Live — speak now');
        }
        return;
      default:
        // 'idle' — healthy torn-down upstream; our attach wakes it (the
        // connecting frame follows). Nothing to render yet.
        return;
    }
  }

  // ─── mic capture ────────────────────────────────────────────

  private async startMic(gen: number): Promise<void> {
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

    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    // R10: the permission prompt is an await — if the attempt died while it
    // was up (disconnect, timeout, replacement), the grant must not leave a
    // live capture behind.
    if (gen !== this.attemptGen) {
      for (const t of stream.getTracks()) t.stop();
      return;
    }
    this.micStream = stream;

    if (!this.audioCtx || this.audioCtx.state === 'closed') {
      this.audioCtx = new AudioContext();
    }
    if (this.audioCtx.state === 'suspended') {
      await this.audioCtx.resume();
      if (gen !== this.attemptGen) {
        // Stop OUR capture only — this.micStream may already belong to a
        // newer attempt (its connect() re-ran stopMic + startMic).
        for (const t of stream.getTracks()) t.stop();
        if (this.micStream === stream) this.micStream = null;
        return;
      }
    }

    const ctx = this.audioCtx;
    const source = ctx.createMediaStreamSource(this.micStream);
    const processor = ctx.createScriptProcessor(this.captureBuf, 1, 1);
    this.processor = processor;

    processor.onaudioprocess = (e: AudioProcessingEvent) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
      // Muted: keep the processor running (it must stay in the graph) but
      // don't send mic audio to the agent.
      if (this.micMuted) return;
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

    if (msg?.type === 'agent.state') {
      this.handleAgentState(msg as AgentStateV1);
    } else if (msg?.type === 'session.config' && msg.audioFormat) {
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
    // Deafened: drop the agent's audio (like a call deafen — you don't hear
    // what was said while deafened).
    if (this.deafened) return;
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
