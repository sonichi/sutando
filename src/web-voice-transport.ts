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
  /** Byte + audio-health counters, ~2×/s, for a stats panel. Everything beyond
   *  `bytesSent`/`bytesRecv` is additive (P7 D7.1) — older consumers keep
   *  reading just the byte totals. */
  onStats?(stats: VoiceStats): void;
  /**
   * Capture-health lifecycle (P7 D7.5 failure contract). 'recovering' when a
   * suspension / device loss triggers the bounded recovery FSM, 'recovered' on
   * success, 'degraded' when recovery is exhausted. Degraded is NOT terminal
   * and deliberately not `onConnectFailure`: the socket and the voice lease
   * stay live (the call is alive, input is not) — surfaces render a distinct
   * mic-error state with a retry affordance wired to `retryCapture()`.
   */
  onCaptureHealth?(
    state: 'recovering' | 'recovered' | 'degraded',
    kind: 'resume' | 'reacquire',
    detail?: string,
  ): void;
}

/** Recovery FSM state (P7 D7.5): recovered collapses back into 'observing'. */
export type CaptureState = 'observing' | 'recovering' | 'degraded';

/**
 * Extended stats snapshot assembled on the 500 ms stats tick (never on the
 * frame path). All counters are scoped to the current connection epoch — they
 * reset on every `connect()`.
 */
export interface VoiceStats {
  bytesSent: number;
  bytesRecv: number;
  /** Capture callbacks seen (counts even while muted — mute must not read as
   *  a capture gap). */
  capCallbacks: number;
  /** Frames skipped because the socket's bufferedAmount exceeded the egress
   *  watermark (backpressure made visible instead of deepening the stall). */
  sendSkipped: number;
  /** Frames whose ws.send threw (socket died mid-callback). */
  sendFailed: number;
  chunksRecv: number;
  /** Chunks handed to the audio graph (schedule-time — NOT proof of playback). */
  chunksScheduled: number;
  /** Natural playback completions only (cancellations counted separately). */
  chunksEnded: number;
  /** Sources stopped by flushPlayback (barge-in/deafen/teardown) — `onended`
   *  fires for these too, so they must not masquerade as completions. */
  chunksCancelled: number;
  scheduledDepth: number;
  lastEndedAt: number | null;
  ctxState: string | null;
  ctxTimeMs: number | null;
  ctxSuspendCount: number;
  captureState: CaptureState;
  /** Watchdog: capture gap past max(3× frame interval, 1 s) while unmuted +
   *  connected, still open. */
  capStalled: boolean;
  lastCapAgoMs: number | null;
  /** Latest frame RMS (subsampled). Client-side corroboration only — the
   *  server's ingress tracker is the canonical speech evidence (D7.1). */
  rms: number;
  speechActive: boolean;
  bufferedAmount: number;
  bufferedHighWater: number;
  /** Per-connection nonce echoed in every audio_health heartbeat (Tranche A:
   *  the engine mints the server epoch and maps this nonce to it on first
   *  sight). */
  epochNonce: string;
  /** D7.5 lifecycle evidence, epoch-retained (bounded rings; `*Dropped`
   *  makes truncation visible instead of silent). */
  ctxLastTransition: { from: string; to: string; at: number } | null;
  deviceEvents: Array<{ kind: string; at: number }>;
  deviceEventsDropped: number;
  recoveryEvents: Array<{ kind: string; result: string; attempt: number; at: number }>;
  recoveryEventsDropped: number;
}

/**
 * One latched episode interval (P7 D7.1). Slots are preallocated and mutated
 * in place — episode latching happens on the frame path, where allocation is
 * forbidden (§D7.0b). `kind: 'gap'` = capture-callback gap; `'speech'` = an
 * above-floor RMS interval. Times are ms relative to the connection epoch.
 */
interface EpisodeSlot {
  /** Episode id (1-based, per epoch). 0 = slot never written. */
  id: number;
  kind: 'gap' | 'speech';
  startMs: number;
  endMs: number;
  durationMs: number;
  /** Speech only: capCallbacks seq at onset/offset. */
  onsetSeq: number;
  offsetSeq: number;
  /** Speech only: max RMS over the interval, in permille (0–1000). */
  maxRmsPm: number;
  aboveFloorMs: number;
  /** Accounted: included in a transmitted heartbeat, or already counted into
   *  episodeOverflow (an unsent slot being evicted or aging out of the send
   *  window is evidence loss). */
  sent: boolean;
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

// ─── P7 D7.1/D7.5 audio-health constants ─────────────────────

/** RMS floor above which a capture frame counts as speech evidence
 *  (client-side corroboration only; the server ingress tracker is canonical). */
export const SPEECH_RMS_FLOOR = 0.02;

/** A speech interval closes after this long below the floor — the hangover
 *  keeps word gaps from splitting one utterance into many episodes. */
export const SPEECH_OFFSET_HANG_MS = 600;

/** Capture-gap watchdog floor: a gap counts as a stall only past
 *  max(3× expected callback interval, this) while unmuted + connected. */
export const CAP_STALL_FLOOR_MS = 1000;

/** Egress backpressure watermark: above this bufferedAmount, capture frames
 *  are skipped (visible in `sendSkipped`) instead of piling more PCM onto a
 *  stalled socket (FE-1). */
export const SEND_BUFFER_WATERMARK_BYTES = 256 * 1024;

/** Hard cap on one serialized audio_health frame. Assembly drops the oldest
 *  window episodes until the frame fits, so the cap holds by construction
 *  (≤300 B / 2 s = ≤150 B/s average on the shared socket). */
export const AUDIO_HEALTH_MAX_BYTES = 300;

/** Heartbeat cadence in 500 ms stats ticks (4 → one per 2 s). The first fires
 *  on the first tick so the engine learns the nonce right after going live. */
export const AUDIO_HEALTH_INTERVAL_TICKS = 4;

/** Preallocated episode retention ring (per connection epoch). */
export const EPISODE_RING_SIZE = 8;

/** Newest episodes re-sent idempotently in every heartbeat (server dedups by
 *  id; ACK-free by design — a server→client ack would queue ahead of PCM). */
export const AUDIO_HEALTH_EPISODE_WINDOW = 4;

/** Bounded single-flight capture recovery (D7.5). */
export const RECOVERY_MAX_ATTEMPTS = 3;
export const RECOVERY_BACKOFF_MS: readonly number[] = [0, 1000, 4000];
export const RECOVERY_RESUME_TIMEOUT_MS = 2000;

/** 8-char per-connection nonce. Tranche A epoch scheme (D7.1): the engine
 *  mints the server-issued epoch and maps it to this nonce on first heartbeat
 *  sight — collision-resistant randomness, no protocol change. */
function mintNonce(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  if (c?.randomUUID) return c.randomUUID().replace(/-/g, '').slice(0, 8);
  return Math.random().toString(36).slice(2, 10).padEnd(8, '0');
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
  /** Speech-evidence RMS floor override (tests). Default SPEECH_RMS_FLOOR. */
  speechRmsFloor?: number;
  /** Speech offset hangover override (tests). Default SPEECH_OFFSET_HANG_MS. */
  speechOffsetHangMs?: number;
  /** Egress skip watermark override (tests). Default SEND_BUFFER_WATERMARK_BYTES. */
  sendBufferWatermark?: number;
  /** Recovery pacing overrides (tests). Defaults RECOVERY_BACKOFF_MS /
   *  RECOVERY_RESUME_TIMEOUT_MS. */
  recoveryBackoffMs?: readonly number[];
  recoveryResumeTimeoutMs?: number;
  /** Clock seam for deterministic watchdog/episode tests. Default Date.now. */
  nowFn?: () => number;
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
  /** `cancelled` distinguishes flushPlayback stops from natural completions —
   *  stop() fires `onended` too, and cancellation is not completion (D7.1). */
  private activeSources: Array<{ src: AudioBufferSourceNode; cancelled: boolean }> = [];
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
  // not as protocol state — the surface reads byte totals from onStats. Since
  // P7 they are epoch-scoped (reset per connect), so the traces re-arm per
  // attempt.
  private audioChunksRecv = 0;
  private micSendCount = 0;

  // ── P7 D7.1 audio-progress ledger (all epoch-scoped; resetLedger) ──
  private speechRmsFloor: number;
  private speechOffsetHangMs: number;
  private sendBufferWatermark: number;
  private recoveryBackoffMs: readonly number[];
  private recoveryResumeTimeoutMs: number;
  private nowFn: () => number;

  private epochNonce = '';
  private connectAtMs = 0;
  private capCallbacks = 0;
  private sendSkipped = 0;
  private sendFailed = 0;
  private chunksScheduled = 0;
  private chunksEnded = 0;
  private chunksCancelled = 0;
  private lastEndedAt: number | null = null;
  private bufferedHighWater = 0;
  private lastCapAt = 0;
  /** captureBuf / ctx.sampleRate, computed when the graph wires (~43 ms). */
  private expectedFrameMs = 43;
  /** max(3× expectedFrameMs, CAP_STALL_FLOOR_MS), fixed at graph wire so the
   *  frame path reads one preallocated field (§D7.0b). */
  private stallAfterMs = CAP_STALL_FLOOR_MS;
  private capStalled = false;
  /** Absolute ms the open capture gap started at (0 = no open gap). */
  private gapOpenedAt = 0;
  private lastRms = 0;
  private speechActive = false;
  private speechOnsetSeq = 0;
  private speechOnsetAtMs = 0;
  private speechMaxRms = 0;
  private speechAboveFloorMs = 0;
  private speechLastAboveAt = 0;
  private ctxSuspendCount = 0;
  private ctxLastTransition: { from: string; to: string; at: number } | null = null;
  private readonly episodeRing: EpisodeSlot[];
  private episodeSeq = 0;
  private episodeOverflow = 0;
  private statsTickCount = 0;
  private heartbeatsSent = 0;
  /** Counter values at the last SUCCESSFULLY SENT heartbeat — the wire
   *  carries deltas against these (D7.1 compact schema). Advanced only on
   *  send success, so a failed frame's interval folds into the next one. */
  private readonly hbPrev = {
    capCallbacks: 0,
    bytesSent: 0,
    sendSkipped: 0,
    sendFailed: 0,
    chunksRecv: 0,
    chunksScheduled: 0,
    chunksEnded: 0,
    chunksCancelled: 0,
  };
  private deviceEvents: Array<{ kind: string; at: number }> = [];
  private deviceEventsDropped = 0;
  private recoveryEvents: Array<{ kind: string; result: string; attempt: number; at: number }> = [];
  private recoveryEventsDropped = 0;

  // ── P7 D7.5 capture recovery FSM ──
  /** Independent of attemptGen (bumping the attempt would kill the still-live
   *  socket's callbacks): bumps on every recovery attempt, on timeout, and in
   *  teardownAudio; every async capture continuation validates BOTH gens. */
  private captureGen = 0;
  private captureState: CaptureState = 'observing';
  /** attemptGen that owns the in-flight recovery loop (-1 = none). Ownership
   *  is attempt-scoped: a stale loop (owner ≠ current attempt) is fenced-dead
   *  and must not swallow the live attempt's triggers. */
  private recoveryOwner = -1;
  /** A reacquire trigger arrived while a resume-recovery was in flight. */
  private recoveryEscalate = false;
  /** Sorted input-device fingerprint (null = cannot enumerate). */
  private inputDeviceSig: string | null = null;
  private deviceChangeHandler: (() => void) | null = null;

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
    this.speechRmsFloor = opts.speechRmsFloor ?? SPEECH_RMS_FLOOR;
    this.speechOffsetHangMs = opts.speechOffsetHangMs ?? SPEECH_OFFSET_HANG_MS;
    this.sendBufferWatermark = opts.sendBufferWatermark ?? SEND_BUFFER_WATERMARK_BYTES;
    this.recoveryBackoffMs = opts.recoveryBackoffMs ?? RECOVERY_BACKOFF_MS;
    this.recoveryResumeTimeoutMs = opts.recoveryResumeTimeoutMs ?? RECOVERY_RESUME_TIMEOUT_MS;
    this.nowFn = opts.nowFn ?? Date.now;
    // Preallocated: episode latching happens on the frame path, where
    // allocation is forbidden (§D7.0b) — slots are mutated in place.
    this.episodeRing = Array.from({ length: EPISODE_RING_SIZE }, () => ({
      id: 0,
      kind: 'gap' as const,
      startMs: 0,
      endMs: 0,
      durationMs: 0,
      onsetSeq: 0,
      offsetSeq: 0,
      maxRmsPm: 0,
      aboveFloorMs: 0,
      sent: false,
    }));
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
    // New connection epoch (D7.1): every counter/episode resets and a fresh
    // nonce is minted, so ledger evidence can never span two calls.
    this.resetLedger();

    this.status('connecting', 'Connecting…');

    // Create the AudioContext up front (ideally on a user gesture) so playback
    // and capture share one clock and it isn't born suspended.
    if (!this.audioCtx || this.audioCtx.state === 'closed') {
      this.audioCtx = new AudioContext();
    }
    this.adoptCtx(this.audioCtx);
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
   *  during voice). False when no socket is open or the frame did not go out. */
  sendTextInput(text: string): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    try {
      this.ws.send(JSON.stringify({ type: 'text_input', text }));
    } catch {
      // readyState is sampled a statement earlier; a close landing in that
      // window throws, and callers read false — not a throw — as "not sent".
      return false;
    }
    return true;
  }

  /** Send a surface-owned protocol command (e.g. voice.retryUpstream), JSON-
   *  serialized onto the open socket; false when no socket is open or the
   *  frame did not go out. */
  sendClientCommand(msg: { type: string }): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    try {
      this.ws.send(JSON.stringify(msg));
    } catch {
      // readyState is sampled a statement earlier; a close landing in that
      // window throws, and callers read false — not a throw — as "not sent".
      return false;
    }
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
    // D7.5: fence every in-flight recovery continuation and tear down the
    // lifecycle listeners BEFORE closing the ctx — close() fires its own
    // statechange, which must not trigger recovery.
    this.captureGen++;
    this.recoveryEscalate = false;
    this.removeDeviceChangeListener();
    if (this.audioCtx) this.audioCtx.onstatechange = null;
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
      // 500 ms cadence, off the frame path (§D7.0b): watchdog + stats +
      // (every 4th tick) the audio_health heartbeat all ride this timer.
      this.statsTimer = setInterval(() => this.runStatsTick(), 500);
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

    if (!this.audioCtx || this.audioCtx.state === 'closed') {
      this.audioCtx = new AudioContext();
    }
    if (this.audioCtx.state === 'suspended') {
      await this.audioCtx.resume();
      if (gen !== this.attemptGen) {
        // Stop OUR capture only — this.micStream may already belong to a
        // newer attempt (its connect() re-ran stopMic + startMic).
        for (const t of stream.getTracks()) t.stop();
        return;
      }
    }

    this.wireCaptureGraph(stream);
  }

  /**
   * Wire (or re-wire, on a recovery reacquire) the capture graph over an
   * acquired stream. Synchronous — callers have already generation-fenced
   * their awaits.
   */
  private wireCaptureGraph(stream: MediaStream): void {
    this.micStream = stream;
    const ctx = this.audioCtx!;
    this.adoptCtx(ctx);
    this.expectedFrameMs = Math.max(1, Math.round((this.captureBuf / ctx.sampleRate) * 1000));
    this.stallAfterMs = Math.max(3 * this.expectedFrameMs, CAP_STALL_FLOOR_MS);
    // Arm the gap clock at wire time: a graph whose FIRST callback never
    // fires must still produce a gap (baseline = the moment capture should
    // have started flowing).
    this.lastCapAt = this.nowFn();
    const source = ctx.createMediaStreamSource(stream);
    const processor = ctx.createScriptProcessor(this.captureBuf, 1, 1);
    this.processor = processor;

    processor.onaudioprocess = (e: AudioProcessingEvent) => {
      // §D7.0b FRAME PATH: O(1) counter/latch writes plus one subsampled RMS
      // pass on top of the pinned downsample+encode+send — no allocation
      // beyond the pinned encode, no I/O beyond the pinned send.
      const now = this.nowFn();
      this.capCallbacks++;
      if (this.gapOpenedAt > 0) {
        this.closeGapEpisode(now);
      } else if (this.lastCapAt > 0 && now - this.lastCapAt > this.stallAfterMs) {
        // Foreground-overwrite (round-1 #7): when the whole thread was
        // frozen, the watchdog timer froze WITH it — the resumed callback is
        // the first code to observe the outage, and it must latch the gap
        // BEFORE overwriting lastCapAt. O(1) preallocated-slot writes.
        this.gapOpenedAt = this.lastCapAt;
        this.closeGapEpisode(now);
      }
      this.lastCapAt = now;
      const raw = e.inputBuffer.getChannelData(0);
      let sum = 0;
      let n = 0;
      for (let i = 0; i < raw.length; i += 4) {
        const v = raw[i];
        sum += v * v;
        n++;
      }
      const rms = n > 0 ? Math.sqrt(sum / n) : 0;
      this.lastRms = rms;
      this.trackSpeech(rms, now);
      // Capture-health accounting above runs regardless of socket/mute state
      // (capture health ≠ socket health); the send path gates below.
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
      // Muted: keep the processor running (it must stay in the graph) but
      // don't send mic audio to the agent.
      if (this.micMuted) return;
      const down = downsample(raw, ctx.sampleRate, this.inputRate);
      const pcm = float32ToInt16(down);
      const buffered = this.ws.bufferedAmount || 0;
      if (buffered > this.bufferedHighWater) this.bufferedHighWater = buffered;
      if (buffered > this.sendBufferWatermark) {
        // Backpressure: more PCM onto a stalled socket only deepens the
        // stall (FE-1) — skip visibly instead.
        this.sendSkipped++;
        return;
      }
      // pcm.buffer is a freshly-allocated ArrayBuffer (float32ToInt16 does
      // `new Int16Array(len)`), so the ArrayBufferLike→ArrayBuffer cast is safe.
      try {
        this.ws.send(pcm.buffer as ArrayBuffer);
      } catch {
        // A socket dying mid-callback must never throw off the audio graph.
        this.sendFailed++;
        return;
      }
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

    // D7.5: device-loss signals drive the recovery FSM.
    const track = stream.getAudioTracks()[0];
    if (track) {
      track.enabled = !this.micMuted; // a reacquired track must honor the mute
      track.onended = () => {
        if (this.micStream !== stream || !this.attemptActive || this.terminal) return;
        this.recordDeviceEvent('track-ended');
        void this.startRecovery('reacquire');
      };
    }
    this.installDeviceChangeListener();
    void this.snapshotInputDevices().then((sig) => {
      if (this.micStream === stream) this.inputDeviceSig = sig;
    });
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
      this.micStream.getTracks().forEach((t) => {
        // Clear the D7.5 listener first — a deliberate stop() must not fire
        // `ended` into the recovery FSM.
        t.onended = null;
        t.stop();
      });
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
      this.adoptCtx(this.audioCtx);
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
      const entry = { src, cancelled: false };
      this.activeSources.push(entry);
      src.onended = () => {
        // Natural completion only — a flushPlayback stop() also fires
        // onended, but that chunk was cancelled, not played out (D7.1).
        if (!entry.cancelled) {
          this.chunksEnded++;
          this.lastEndedAt = this.nowFn();
        }
        const idx = this.activeSources.indexOf(entry);
        if (idx >= 0) this.activeSources.splice(idx, 1);
      };
      this.chunksScheduled++;
      if (this.chunksScheduled <= 5) {
        this.debug(
          'Played chunk #' + this.chunksScheduled + ': ' + f32.length +
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
    for (const entry of this.activeSources) {
      // Counted HERE, deterministically — not in onended (which a closed ctx
      // may never fire). The flag keeps a trailing onended from also counting
      // the chunk as ended.
      entry.cancelled = true;
      this.chunksCancelled++;
      try {
        entry.src.stop();
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

  // ─── P7 D7.1 audio-progress ledger ──────────────────────────

  /** New connection epoch: zero every counter, clear the episode ring, mint
   *  the nonce the engine maps to its server-issued epoch on first heartbeat
   *  sight. Ledger evidence can never span two calls. */
  private resetLedger(): void {
    this.epochNonce = mintNonce();
    this.connectAtMs = this.nowFn();
    this.bytesSent = 0;
    this.bytesRecv = 0;
    this.audioChunksRecv = 0;
    this.micSendCount = 0;
    this.capCallbacks = 0;
    this.sendSkipped = 0;
    this.sendFailed = 0;
    this.chunksScheduled = 0;
    this.chunksEnded = 0;
    this.chunksCancelled = 0;
    this.lastEndedAt = null;
    this.bufferedHighWater = 0;
    this.lastCapAt = 0;
    this.capStalled = false;
    this.gapOpenedAt = 0;
    this.lastRms = 0;
    this.speechActive = false;
    this.speechMaxRms = 0;
    this.speechAboveFloorMs = 0;
    this.ctxSuspendCount = 0;
    this.ctxLastTransition = null;
    for (const slot of this.episodeRing) {
      slot.id = 0;
      slot.sent = false;
    }
    this.episodeSeq = 0;
    this.episodeOverflow = 0;
    this.statsTickCount = 0;
    this.heartbeatsSent = 0;
    this.hbPrev.capCallbacks = 0;
    this.hbPrev.bytesSent = 0;
    this.hbPrev.sendSkipped = 0;
    this.hbPrev.sendFailed = 0;
    this.hbPrev.chunksRecv = 0;
    this.hbPrev.chunksScheduled = 0;
    this.hbPrev.chunksEnded = 0;
    this.hbPrev.chunksCancelled = 0;
    this.deviceEvents = [];
    this.deviceEventsDropped = 0;
    this.recoveryEvents = [];
    this.recoveryEventsDropped = 0;
    this.captureState = 'observing';
    this.inputDeviceSig = null;
  }

  /** Latched speech intervals (D7.1): an utterance entirely between
   *  heartbeats still leaves its record. O(1) — frame-path safe. */
  private trackSpeech(rms: number, now: number): void {
    if (rms >= this.speechRmsFloor) {
      if (!this.speechActive) {
        this.speechActive = true;
        this.speechOnsetSeq = this.capCallbacks;
        this.speechOnsetAtMs = now;
        this.speechMaxRms = 0;
        this.speechAboveFloorMs = 0;
      }
      if (rms > this.speechMaxRms) this.speechMaxRms = rms;
      this.speechAboveFloorMs += this.expectedFrameMs;
      this.speechLastAboveAt = now;
    } else if (this.speechActive && now - this.speechLastAboveAt >= this.speechOffsetHangMs) {
      this.speechActive = false;
      this.latchEpisode('speech', this.speechOnsetAtMs, this.speechLastAboveAt);
    }
  }

  /** Write the next preallocated ring slot (frame-path safe: field writes
   *  only). Evicting a slot that never made a heartbeat is evidence loss —
   *  it bumps episodeOverflow, which the matrix maps to
   *  insufficient-evidence. */
  private latchEpisode(kind: 'gap' | 'speech', startAbsMs: number, endAbsMs: number): void {
    const slot = this.episodeRing[this.episodeSeq % EPISODE_RING_SIZE];
    if (slot.id !== 0 && !slot.sent) this.episodeOverflow++;
    this.episodeSeq++;
    slot.id = this.episodeSeq;
    slot.kind = kind;
    slot.startMs = Math.max(0, Math.round(startAbsMs - this.connectAtMs));
    slot.endMs = Math.max(0, Math.round(endAbsMs - this.connectAtMs));
    slot.durationMs = Math.max(0, Math.round(endAbsMs - startAbsMs));
    slot.sent = false;
    if (kind === 'speech') {
      slot.onsetSeq = this.speechOnsetSeq;
      slot.offsetSeq = this.capCallbacks;
      slot.maxRmsPm = Math.min(1000, Math.round(this.speechMaxRms * 1000));
      slot.aboveFloorMs = Math.round(this.speechAboveFloorMs);
    } else {
      slot.onsetSeq = 0;
      slot.offsetSeq = 0;
      slot.maxRmsPm = 0;
      slot.aboveFloorMs = 0;
    }
  }

  /** Close the open capture gap into a latched episode (frame-path safe). */
  private closeGapEpisode(now: number): void {
    this.latchEpisode('gap', this.gapOpenedAt, now);
    this.gapOpenedAt = 0;
    this.capStalled = false;
  }

  /**
   * 500 ms cadence, off the frame path (§D7.0b): the capture watchdog, the
   * extended stats publication, and — every AUDIO_HEALTH_INTERVAL_TICKS-th
   * tick, starting with the first so the engine learns the nonce right after
   * going live — audio_health assembly + send.
   */
  private runStatsTick(): void {
    const now = this.nowFn();
    // Watchdog (D7.1): a silent capture past max(3× frame interval, 1 s)
    // while unmuted + connected is a stall. The gap LATCHES as an episode
    // when capture returns (the frame path also self-latches, for outages
    // the frozen timer never saw); a permanent stall travels as the open
    // gap (`og`). Armed while a processor exists OR recovery is in flight —
    // a reacquire outage (stopMic cleared the processor) is still a gap.
    if (
      (this.processor || this.captureState !== 'observing') &&
      !this.micMuted &&
      this.ws &&
      this.ws.readyState === WebSocket.OPEN &&
      this.lastCapAt > 0 &&
      this.gapOpenedAt === 0 &&
      now - this.lastCapAt > this.stallAfterMs
    ) {
      this.gapOpenedAt = this.lastCapAt;
      this.capStalled = true;
    }
    this.ev.onStats?.(this.buildStats(now));
    if (this.statsTickCount % AUDIO_HEALTH_INTERVAL_TICKS === 0) this.sendAudioHealth(now);
    this.statsTickCount++;
  }

  private buildStats(now: number): VoiceStats {
    return {
      bytesSent: this.bytesSent,
      bytesRecv: this.bytesRecv,
      capCallbacks: this.capCallbacks,
      sendSkipped: this.sendSkipped,
      sendFailed: this.sendFailed,
      chunksRecv: this.audioChunksRecv,
      chunksScheduled: this.chunksScheduled,
      chunksEnded: this.chunksEnded,
      chunksCancelled: this.chunksCancelled,
      scheduledDepth: this.activeSources.length,
      lastEndedAt: this.lastEndedAt,
      ctxState: this.audioCtx?.state ?? null,
      ctxTimeMs: this.audioCtx ? Math.round(this.audioCtx.currentTime * 1000) : null,
      ctxSuspendCount: this.ctxSuspendCount,
      captureState: this.captureState,
      capStalled: this.capStalled,
      lastCapAgoMs: this.lastCapAt > 0 ? now - this.lastCapAt : null,
      rms: this.lastRms,
      speechActive: this.speechActive,
      bufferedAmount: this.ws ? this.ws.bufferedAmount || 0 : 0,
      bufferedHighWater: this.bufferedHighWater,
      epochNonce: this.epochNonce,
      ctxLastTransition: this.ctxLastTransition,
      deviceEvents: [...this.deviceEvents],
      deviceEventsDropped: this.deviceEventsDropped,
      recoveryEvents: [...this.recoveryEvents],
      recoveryEventsDropped: this.recoveryEventsDropped,
    };
  }

  /**
   * The 2 s audio_health heartbeat (D7.1). Compact wire schema, hard-capped
   * at AUDIO_HEALTH_MAX_BYTES by construction: window episodes are dropped
   * oldest-first until the frame fits (an entry evicted from the ring without
   * ever being sent surfaces via `eo`). Counters are absolute — idempotent
   * across skipped heartbeats; episodes re-send idempotently and the server
   * dedups by id. There are NO server→client telemetry frames (§D7.0b).
   *
   * Wire schema (short keys; optional keys omitted when empty):
   *   t   'audio_health'
   *   n   epoch nonce (engine maps nonce→server epoch on first sight)
   *   q   heartbeat seq within the epoch
   *   ea  epoch age: ms since connect() on the CLIENT clock at assembly —
   *       lets the engine place epoch-relative episode intervals on its own
   *       clock as receivedAt − ea (accurate to network latency), instead of
   *       guessing the epoch start from first-heartbeat timing
   *   c   DELTAS since the last successfully-sent heartbeat (lossless: a
   *       failed/skipped frame's interval folds into the next delta):
   *       [capCallbacks, bytesSent, sendSkipped, sendFailed, chunksRecv,
   *        chunksScheduled, chunksEnded, chunksCancelled]
   *   x   [ctxTimeMs|-1, scheduledDepth, lastEndedAgoMs|-1]
   *   cs  ctx state initial ('r'unning | 's'uspended | 'c'losed)
   *   cap capture state initial ('o'bserving | 'r'ecovering | 'd'egraded)
   *   sc  ctxSuspendCount
   *   ba  [bufferedAmount, bufferedHighWater]
   *   mu  1 while mic muted
   *   og  [gapStartRelMs, ageMs] while a capture gap is OPEN — a permanent
   *       stall never closes an episode, so the open gap itself must travel
   *   eo  episodes evicted unsent (matrix: insufficient-evidence)
   *   ep  ≤4 entries, oldest→newest:
   *       gap    [id, 'g', startRelMs, durationMs]
   *       speech [id, 's', onsetSeq, offsetSeq, maxRmsPm, aboveFloorMs]
   *
   * The ≤300 B cap holds by construction: episodes trim oldest-first, and if
   * the episode-free frame still exceeds the cap, diagnostic fields drop in
   * the order x → ba → sc (never the core evidence: n/q/c/cs/cap/mu/og/eo).
   */
  private sendAudioHealth(now: number): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    const frame: Record<string, unknown> = {
      t: 'audio_health',
      n: this.epochNonce,
      q: this.heartbeatsSent,
      ea: Math.max(0, Math.round(now - this.connectAtMs)),
      c: [
        this.capCallbacks - this.hbPrev.capCallbacks,
        this.bytesSent - this.hbPrev.bytesSent,
        this.sendSkipped - this.hbPrev.sendSkipped,
        this.sendFailed - this.hbPrev.sendFailed,
        this.audioChunksRecv - this.hbPrev.chunksRecv,
        this.chunksScheduled - this.hbPrev.chunksScheduled,
        this.chunksEnded - this.hbPrev.chunksEnded,
        this.chunksCancelled - this.hbPrev.chunksCancelled,
      ],
      x: [
        this.audioCtx ? Math.round(this.audioCtx.currentTime * 1000) : -1,
        this.activeSources.length,
        this.lastEndedAt != null ? Math.max(0, now - this.lastEndedAt) : -1,
      ],
      cs: (this.audioCtx?.state ?? 'x')[0],
      cap: this.captureState[0],
    };
    if (this.ctxSuspendCount > 0) frame.sc = this.ctxSuspendCount;
    const buffered = this.ws.bufferedAmount || 0;
    if (buffered > 0 || this.bufferedHighWater > 0) {
      frame.ba = [buffered, this.bufferedHighWater];
    }
    if (this.micMuted) frame.mu = 1;
    if (this.gapOpenedAt > 0) {
      frame.og = [
        Math.max(0, Math.round(this.gapOpenedAt - this.connectAtMs)),
        Math.max(0, Math.round(now - this.gapOpenedAt)),
      ];
    }

    // Newest AUDIO_HEALTH_EPISODE_WINDOW episodes, oldest→newest on the wire.
    const window: EpisodeSlot[] = [];
    for (let i = 0; i < AUDIO_HEALTH_EPISODE_WINDOW; i++) {
      const seq = this.episodeSeq - i;
      if (seq <= 0) break;
      const slot = this.episodeRing[(seq - 1) % EPISODE_RING_SIZE];
      if (slot.id === seq) window.push(slot);
    }
    window.reverse();
    // An unsent episode that has aged OUT of the window can never be sent —
    // that is overflow now, not at ring eviction (the "5th unsent episode"
    // rule). Marked accounted so it counts exactly once.
    for (const slot of this.episodeRing) {
      if (slot.id > 0 && !slot.sent && slot.id <= this.episodeSeq - AUDIO_HEALTH_EPISODE_WINDOW) {
        this.episodeOverflow++;
        slot.sent = true;
      }
    }
    if (this.episodeOverflow > 0) frame.eo = this.episodeOverflow;

    const enc = new TextEncoder();
    let payload: string;
    for (;;) {
      if (window.length > 0) {
        frame.ep = window.map((s) =>
          s.kind === 'gap'
            ? [s.id, 'g', s.startMs, s.durationMs]
            : [s.id, 's', s.onsetSeq, s.offsetSeq, s.maxRmsPm, s.aboveFloorMs],
        );
      } else {
        delete frame.ep;
      }
      payload = JSON.stringify(frame);
      if (enc.encode(payload).byteLength <= AUDIO_HEALTH_MAX_BYTES || window.length === 0) break;
      window.shift(); // drop oldest — it re-sends next beat (or surfaces in eo)
    }
    // Episode-free frame still over the cap (extreme counter widths): drop
    // diagnostics in fixed order, keeping the core evidence intact.
    for (const key of ['x', 'ba', 'sc'] as const) {
      if (enc.encode(payload).byteLength <= AUDIO_HEALTH_MAX_BYTES) break;
      if (frame[key] !== undefined) {
        delete frame[key];
        payload = JSON.stringify(frame);
      }
    }
    try {
      this.ws.send(payload);
    } catch {
      return; // heartbeat loss is itself evidence; never throw off a timer
    }
    this.heartbeatsSent++;
    for (const s of window) s.sent = true;
    // Advance the delta baseline only after a SUCCESSFUL send.
    this.hbPrev.capCallbacks = this.capCallbacks;
    this.hbPrev.bytesSent = this.bytesSent;
    this.hbPrev.sendSkipped = this.sendSkipped;
    this.hbPrev.sendFailed = this.sendFailed;
    this.hbPrev.chunksRecv = this.audioChunksRecv;
    this.hbPrev.chunksScheduled = this.chunksScheduled;
    this.hbPrev.chunksEnded = this.chunksEnded;
    this.hbPrev.chunksCancelled = this.chunksCancelled;
  }

  // ─── P7 D7.5 capture recovery FSM ───────────────────────────

  /** Attach lifecycle observation to a (possibly fresh) AudioContext.
   *  Idempotent; teardownAudio clears it before close so the close's own
   *  statechange never triggers recovery. */
  private adoptCtx(ctx: AudioContext): void {
    ctx.onstatechange = () => this.handleCtxStateChange(ctx);
  }

  private handleCtxStateChange(ctx: AudioContext): void {
    if (ctx !== this.audioCtx) return; // stale context
    const to = ctx.state;
    const from = this.ctxLastTransition?.to ?? 'running';
    this.ctxLastTransition = { from, to, at: this.nowFn() };
    if (to === 'suspended') {
      this.ctxSuspendCount++;
      // Unexpected suspension while a live attempt captures → recovery.
      if (this.attemptActive && !this.terminal && this.processor) {
        void this.startRecovery('resume');
      }
    }
  }

  /**
   * Manual retry from the degraded state (the UI affordance in D7.5's
   * failure contract). Re-arms a full bounded reacquire; no-op while a
   * recovery is already in flight or the attempt is not live.
   */
  retryCapture(): void {
    if (!this.attemptActive || this.terminal) return;
    void this.startRecovery('reacquire');
  }

  /**
   * Single-flight bounded capture recovery (D7.5). `captureGen` — independent
   * of `attemptGen`, because bumping the attempt would kill the still-live
   * socket's callbacks — fences every await: it bumps on each attempt, on
   * timeout, and in teardownAudio, so a late resume()/getUserMedia completion
   * from a superseded attempt can never overwrite a newer graph.
   */
  private async startRecovery(kind: 'resume' | 'reacquire'): Promise<void> {
    if (this.recoveryOwner === this.attemptGen) {
      // Coalesce into the LIVE attempt's loop: device loss during a
      // resume-recovery upgrades its next attempt to a full reacquire; a
      // same-kind re-trigger is already covered.
      if (kind === 'reacquire') this.recoveryEscalate = true;
      return;
    }
    // Any other owner is a stale attempt's loop — it is fenced-dead (its gen
    // checks make every remaining await a no-op), so the live attempt takes
    // ownership rather than letting the corpse swallow its trigger.
    this.recoveryOwner = this.attemptGen;
    this.recoveryEscalate = false;
    const gen = this.attemptGen;
    this.captureState = 'recovering';
    this.ev.onCaptureHealth?.('recovering', kind);
    let effectiveKind = kind;
    let failures = 0;
    let iterations = 0;
    try {
      // Failure-counted (an escalated retry after a SUCCESSFUL resume must
      // not burn an attempt); the iteration cap is a belt against a
      // pathological trigger storm re-arming escalation forever.
      while (failures < RECOVERY_MAX_ATTEMPTS && ++iterations <= 10) {
        const backoff =
          this.recoveryBackoffMs[Math.min(failures, this.recoveryBackoffMs.length - 1)] ?? 0;
        if (backoff > 0 && iterations > 1) {
          await new Promise((r) => setTimeout(r, backoff));
          if (gen !== this.attemptGen) return;
        }
        if (this.recoveryEscalate) {
          effectiveKind = 'reacquire';
          this.recoveryEscalate = false;
        }
        const capGen = ++this.captureGen;
        const ok =
          effectiveKind === 'resume'
            ? await this.tryResume(gen, capGen)
            : await this.tryReacquire(gen, capGen);
        if (gen !== this.attemptGen) return;
        if (ok) {
          if (this.recoveryEscalate) {
            // A reacquire request landed while this (resume) pass was in
            // flight: the track it reported on is still dead — keep going,
            // consuming the escalation on the next iteration.
            continue;
          }
          this.recordRecoveryEvent(effectiveKind, 'recovered', failures + 1);
          this.captureState = 'observing';
          this.ev.onCaptureHealth?.('recovered', effectiveKind);
          return;
        }
        failures++;
        this.recordRecoveryEvent(effectiveKind, 'failed', failures);
      }
      // Exhausted (failure contract): degraded, NOT terminal — the socket
      // and the voice lease stay live; the surface renders a retry
      // affordance (retryCapture). P4's evidence ladder sees the stall via
      // the heartbeat, not via a connection failure.
      this.captureState = 'degraded';
      this.ev.onCaptureHealth?.(
        'degraded',
        effectiveKind,
        'Microphone input could not be recovered',
      );
    } finally {
      if (this.recoveryOwner === gen) this.recoveryOwner = -1;
    }
  }

  private async tryResume(gen: number, capGen: number): Promise<boolean> {
    const ctx = this.audioCtx;
    if (!ctx || ctx.state === 'closed') return false;
    try {
      await this.withTimeout(ctx.resume(), this.recoveryResumeTimeoutMs);
    } catch {
      this.captureGen++; // timeout: fence the abandoned resume (D7.5)
      return false;
    }
    if (gen !== this.attemptGen || capGen !== this.captureGen) return false;
    return this.audioCtx === ctx && ctx.state === 'running';
  }

  private async tryReacquire(gen: number, capGen: number): Promise<boolean> {
    // Tear down the dead capture first (keep the ctx — playback drains on it).
    this.stopMic();
    let stream: MediaStream;
    try {
      const p = navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      // A timed-out acquisition can still resolve later — the late grant
      // must not leak a live capture (R10 discipline, captureGen edition;
      // the timeout below bumps captureGen, so this sees it stale).
      p.then((s) => {
        if (gen !== this.attemptGen || capGen !== this.captureGen) {
          for (const t of s.getTracks()) t.stop();
        }
      }).catch(() => {});
      stream = await this.withTimeout(p, this.recoveryResumeTimeoutMs);
    } catch {
      this.captureGen++; // timeout/denial: fence the late completion
      return false;
    }
    if (gen !== this.attemptGen || capGen !== this.captureGen) return false; // tracks stopped above
    if (this.audioCtx && this.audioCtx.state === 'suspended') {
      try {
        await this.withTimeout(this.audioCtx.resume(), this.recoveryResumeTimeoutMs);
      } catch {
        /* verdict below reads the actual ctx state */
      }
      if (gen !== this.attemptGen || capGen !== this.captureGen) {
        for (const t of stream.getTracks()) t.stop();
        return false;
      }
    }
    // Honest verdict: a graph wired onto a non-running context captures
    // nothing — reporting 'recovered' with the ctx still suspended would
    // tell the UI capture is back while it stays dead.
    if (!this.audioCtx || this.audioCtx.state !== 'running') {
      for (const t of stream.getTracks()) t.stop();
      return false;
    }
    this.wireCaptureGraph(stream);
    return true;
  }

  private withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('recovery timeout')), ms);
      p.then(
        (v) => {
          clearTimeout(timer);
          resolve(v);
        },
        (e) => {
          clearTimeout(timer);
          reject(e);
        },
      );
    });
  }

  private recordDeviceEvent(kind: string): void {
    this.deviceEvents.push({ kind, at: this.nowFn() });
    if (this.deviceEvents.length > 8) {
      this.deviceEvents.shift();
      this.deviceEventsDropped++; // truncation is evidence loss — count it
    }
  }

  private recordRecoveryEvent(kind: string, result: string, attempt: number): void {
    this.recoveryEvents.push({ kind, result, attempt, at: this.nowFn() });
    if (this.recoveryEvents.length > 8) {
      this.recoveryEvents.shift();
      this.recoveryEventsDropped++;
    }
  }

  private installDeviceChangeListener(): void {
    if (this.deviceChangeHandler) return; // one per transport
    const md = (navigator as { mediaDevices?: unknown }).mediaDevices as
      | {
          addEventListener?: (type: string, h: () => void) => void;
          ondevicechange?: (() => void) | null;
        }
      | undefined;
    if (!md) return;
    const handler = (): void => {
      void this.handleDeviceChange();
    };
    this.deviceChangeHandler = handler;
    if (typeof md.addEventListener === 'function') md.addEventListener('devicechange', handler);
    else md.ondevicechange = handler;
  }

  private removeDeviceChangeListener(): void {
    const handler = this.deviceChangeHandler;
    this.deviceChangeHandler = null;
    if (!handler) return;
    const md = (navigator as { mediaDevices?: unknown }).mediaDevices as
      | {
          removeEventListener?: (type: string, h: () => void) => void;
          ondevicechange?: (() => void) | null;
        }
      | undefined;
    if (!md) return;
    if (typeof md.removeEventListener === 'function') {
      md.removeEventListener('devicechange', handler);
    } else if (md.ondevicechange === handler) {
      md.ondevicechange = null;
    }
  }

  /** Input-device fingerprint — `devicechange` fires for outputs too, and
   *  only input-set changes justify a reacquire (D7.5). Null = cannot
   *  enumerate (never reacquire blind). */
  private async snapshotInputDevices(): Promise<string | null> {
    const md = (navigator as { mediaDevices?: unknown }).mediaDevices as
      | { enumerateDevices?: () => Promise<Array<{ kind?: string; deviceId?: string }>> }
      | undefined;
    if (!md || typeof md.enumerateDevices !== 'function') return null;
    try {
      const devices = await md.enumerateDevices();
      return devices
        .filter((d) => d?.kind === 'audioinput')
        .map((d) => String(d?.deviceId ?? ''))
        .sort()
        .join('|');
    } catch {
      return null;
    }
  }

  private async handleDeviceChange(): Promise<void> {
    if (!this.attemptActive || this.terminal || !this.processor) return;
    const gen = this.attemptGen;
    const capGen = this.captureGen;
    const sig = await this.snapshotInputDevices();
    // Dual fence (round-1 fix #5): a recovery that replaced the graph while
    // this enumeration was pending bumped captureGen — acting on the stale
    // result would tear the fresh capture down again.
    if (gen !== this.attemptGen || capGen !== this.captureGen) return;
    if (sig === null || this.inputDeviceSig === null) return;
    if (sig === this.inputDeviceSig) return; // output-only change
    this.inputDeviceSig = sig;
    this.recordDeviceEvent('input-devices-changed');
    void this.startRecovery('reacquire');
  }
}
