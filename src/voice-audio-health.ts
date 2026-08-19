// voice-audio-health — P7 D7.1 engine-side audio-progress ledger (Tranche A
// interim). Wraps the session-layer seams only, so coverage is honestly
// 'session-only': frames buffered by bodhi's ClientTransport during a
// reconnect never pass handleAudioFromClient, and the matrix must downgrade
// rather than misfire on that blind window.
//
// §D7.0b budget: the two frame-path wraps (audio ingress, audio egress) do
// only O(1) counter/latch writes plus one subsampled RMS pass — no
// allocation, no I/O, no serialization. Heartbeat ingest is message-rate
// (~0.5/s); snapshots, [Health] segments, and persistence rows are
// assembled on the 30 s / 1-min timers, never on a frame path. Persistence
// itself is a try-enqueue into an injected one-slot mailbox — a busy slot
// skips the sample (visible in `samplesSkipped`), it never queues.

import type { ConnectionLifecycleEvent, UpstreamCounters } from 'bodhi-realtime-agent';

/** Coverage is an ordered level, not a boolean: each level observes everything
 *  the previous one did. 'session+egress' sees what the agent handed the SDK;
 *  'full' (server-side accounting) stays unreachable today. */
export type LedgerCoverage = 'session-only' | 'session+egress' | 'full';
const COVERAGE_RANK: Record<LedgerCoverage, number> = {
  'session-only': 0,
  'session+egress': 1,
  full: 2,
};
export const observesUpstreamSend = (c: LedgerCoverage): boolean =>
  COVERAGE_RANK[c] >= COVERAGE_RANK['session+egress'];

/** What the ledger samples from bodhi's VoiceSession.getDiagnostics(). Null
 *  fields mean UNOBSERVED (injected fakes, older pins) — never zero. */
export interface SessionDiagnosticsSample {
  upstream: UpstreamCounters | null;
  transportGeneration: number | null;
  echoSuppressed: number | null;
}

/** Mirror of the client transport's speech constants: floor above which a
 *  frame counts as speech, and the offset hangover. */
export const ENGINE_SPEECH_RMS_FLOOR = 0.02;
export const ENGINE_SPEECH_HANG_MS = 600;

/** Ingress considered stalled when no frame reached the session for this
 *  long while a client is attached and its heartbeat says unmuted. */
export const INGRESS_STALL_MS = 5000;

/** Client heartbeat considered stale past this (expected cadence 2 s). */
export const HEARTBEAT_STALE_MS = 8000;

/** Bounded payload column (a snapshot serializes ~1 KB; the cap is a
 *  guard, not a target). */
export const PERSIST_PAYLOAD_MAX_BYTES = 4096;

/** The engine-owned speech evidence (round-3 #4: ONE owner, one precedence —
 *  this tracker is canonical; client RMS intervals are corroboration only).
 *  Computed over PCM as delivered to the session, pre-EchoGuard. */
export interface SpeechEvidence {
  active: boolean;
  onsetAt: number | null;
  /** Onset of the most recent utterance, RETAINED after the hangover expires
   *  (cleared only on epoch reset) — the 30s matrix window needs the first
   *  sample of a completed utterance, which live `onsetAt` erases. */
  lastOnsetAt: number | null;
  lastAboveFloorAt: number | null;
}

/** Parsed client audio_health heartbeat (wire schema owned by
 *  web-voice-transport.ts sendAudioHealth — short keys, ≤300 B). Unknown
 *  fields are ignored by construction: only the keys below are read.
 *  The eight counter fields are DELTAS since the client's previous
 *  successfully-sent heartbeat (the ledger accumulates them into
 *  `clientTotals`). */
export interface ClientHeartbeat {
  nonce: string;
  seq: number;
  /** ms since connect() on the CLIENT clock at assembly (null on frames from
   *  clients predating the field). epochStartApprox = receivedAt − ea. */
  epochAgeMs: number | null;
  capCallbacks: number;
  bytesSent: number;
  sendSkipped: number;
  sendFailed: number;
  chunksRecv: number;
  chunksScheduled: number;
  chunksEnded: number;
  chunksCancelled: number;
  ctxTimeMs: number | null;
  scheduledDepth: number | null;
  lastEndedAgoMs: number | null;
  ctxState: string | null;
  captureState: string | null;
  ctxSuspendCount: number;
  bufferedAmount: number | null;
  bufferedHighWater: number | null;
  muted: boolean;
  openGap: { startMs: number; ageMs: number } | null;
  episodeOverflow: number;
  episodes: ClientEpisode[];
  receivedAt: number;
}

/** One episode interval from the wire window. Gap entries carry
 *  {startMs, durationMs} (client-epoch-relative); speech entries carry
 *  {onsetSeq, offsetSeq, maxRmsPm, aboveFloorMs}. */
export interface ClientEpisode {
  id: number;
  kind: 'gap' | 'speech';
  startMs?: number;
  durationMs?: number;
  onsetSeq?: number;
  offsetSeq?: number;
  maxRmsPm?: number;
  aboveFloorMs?: number;
}

/** One persistence row (the injected mailbox writes it to sqlite). */
export interface HealthRow {
  tsUnix: number;
  sessionId: string;
  epoch: number | null;
  nonce: string | null;
  /** 'lineage' rows are §1.1 generation→lineage reconciliation records —
   *  offline analysis re-attributes earlier rows from them instead of
   *  trusting a stale in-row id. */
  reason: 'timer' | 'anomaly' | 'final' | 'lineage';
  payload: string;
}

/** Matrix facts as recorded by the ledger. Structural (not the MatrixFacts
 *  type) to avoid a ledger→matrix import cycle; widened past boolean for
 *  speechObservedAt, the one timestamp member. */
export type MatrixFactsRecord = Record<string, boolean | number | null>;

export interface AudioHealthSnapshot {
  coverage: LedgerCoverage;
  /** Sampled bodhi diagnostics at snapshot time; null = unobserved. */
  upstream: UpstreamCounters | null;
  transportGeneration: number | null;
  echoSuppressed: number | null;
  /** Resumption lineage, derived sutando-side from lifecycle events (design
   *  §1.1). Minted on a handle-less setup-ok; 'suspected-sever' is terminal. */
  logicalSessionId: number;
  lineageState: 'none' | 'fresh' | 'resumed' | 'suspected-sever';
  /** Latest server-reported standing prompt size (promptTokenCount). */
  contextTokens: number | null;
  contextTokensAt: number | null;
  /** Per-modality breakdown of the standing prompt (promptTokensDetails,
   *  design §1.4) — what separates "video filled the context" from
   *  "conversation did". Held and reset with the lineage, like the scalar
   *  it details. Null until reported. */
  contextTokensDetails: Record<string, number> | null;
  epoch: number | null;
  nonce: string | null;
  deliveredFrames: number;
  deliveredBytes: number;
  lastDeliveredAt: number | null;
  egressFrames: number;
  egressBytes: number;
  lastEgressAt: number | null;
  speech: SpeechEvidence;
  ingressRms: number;
  lastHeartbeat: ClientHeartbeat | null;
  /** Delta heartbeats accumulated into per-epoch totals. */
  clientTotals: {
    capCallbacks: number;
    bytesSent: number;
    sendSkipped: number;
    sendFailed: number;
    chunksRecv: number;
    chunksScheduled: number;
    chunksEnded: number;
    chunksCancelled: number;
  };
  heartbeatCount: number;
  newEpisodeIds: number[];
  samplesSkipped: number;
  lastTurnLatencyMs: number | null;
  /** Server-clock ingress-onset → first model EVENT (receipt-time
   *  approximation — D7.3's user-speech→first-model-event metric). */
  lastSpeechToModelMs: number | null;
  /** Latest observed model activity (audio out, turn start, tool call). */
  lastModelEventAt: number | null;
  inputHealth: 'ok' | 'degraded' | 'stalled' | 'no-client' | 'unknown';
  /** Receipt-time approximation of the client epoch's start on the engine
   *  clock (null before the first heartbeat). */
  epochStartApproxMs: number | null;
  /** Last D7.2 matrix result noted via noteMatrixVerdict (persisted with
   *  every row). Facts + reasons make the row a RECORDED DECISION (design
   *  §1.6 option 2): even a stripped row replays as the decision taken.
   *  Facts are structural (Record) to avoid a ledger→matrix import cycle. */
  lastMatrixVerdict: string | null;
  lastMatrixFacts: MatrixFactsRecord | null;
  lastMatrixReasons: string[] | null;
}

export interface AudioHealthLedger {
  /** Install the session-layer wraps (monkey-patch, same pattern as the
   *  narration-tee's handleAudioOutput wrap). Idempotent per session. */
  wrapSession(session: unknown): void;
  /** New client connection = new (pending) epoch: reset per-epoch state.
   *  The epoch value itself is minted on first heartbeat sight. */
  onClientConnected(): void;
  onClientDisconnected(): void;
  noteTurnLatency(totalE2EMs: number | undefined): void;
  /** Any observed model activity beyond audio (turn start, tool call) — the
   *  D7.1 model hop is EVENTS, and audio alone misses text/tool-first turns. */
  noteModelEvent(): void;
  /** Canonical speech evidence — the vision gate and the matrix consume
   *  THIS (precedence: ingress tracker > client RMS intervals). */
  getSpeechEvidence(): SpeechEvidence;
  getInputHealth(clientConnected: boolean): 'ok' | 'degraded' | 'stalled' | 'no-client' | 'unknown';
  getSnapshot(clientConnected: boolean): AudioHealthSnapshot;
  /** Upgraded [Health] segments (30 s tick). Also advances the per-tick rate
   *  windows — call EVERY tick, even when the line is suppressed, or a later
   *  line reports a long-window average instead of the last 30 s. */
  healthSegments(clientConnected: boolean, serverBufferedAmount?: number | null): string;
  /** Anomaly-overrides-suppression (D7.1): true forces a [Health] line even
   *  during an unchanged-ACTIVE streak. PEEK ONLY — call clearTickLatches()
   *  after the anomaly row has been logged AND persisted, so the evidence
   *  cannot be cleared before it is serialized. */
  anomalies(clientConnected: boolean): { anomalous: boolean; reasons: string[] };
  clearTickLatches(): void;
  /** Try-enqueue one row into the persistence mailbox; a busy mailbox skips
   *  the sample (samplesSkipped), never queues (§D7.0b). */
  persistTick(reason: 'timer' | 'anomaly' | 'final', clientConnected: boolean): void;
  /** D7.2: record the latest matrix result — verdict plus facts and reasons
   *  (the recorded decision, design §1.6 option 2) — so every persisted row
   *  carries it. */
  noteMatrixVerdict(verdict: string, facts?: MatrixFactsRecord, reasons?: string[]): void;
  /** Server-reported standing prompt size + per-modality breakdown (bodhi
   *  onUsageMetadata; design §1.4). */
  noteUsageMetadata(
    promptTokenCount: number | null | undefined,
    promptTokensDetails?: ReadonlyArray<{ modality?: string; tokenCount?: number }> | null,
  ): void;
  /** Lineage derivation input (bodhi onConnectionLifecycle) — design §1.1. */
  noteLifecycleEvent(event: ConnectionLifecycleEvent): void;
  /** Exposed for tests (the wrap routes here). */
  ingestHeartbeat(msg: unknown): void;
}

export interface AudioHealthOptions {
  sessionId: string;
  nowFn?: () => number;
  persist?: (row: HealthRow) => boolean;
  log?: (line: string) => void;
  speechRmsFloor?: number;
  speechHangMs?: number;
  /** Samples bodhi's session diagnostics on ledger ticks (never on the frame
   *  path). Absent or throwing ⇒ upstream stays unobserved, up=n/a. */
  getSessionDiagnostics?: () => SessionDiagnosticsSample | null;
}

/** Parse one wire heartbeat; returns null when the shape is not a plausible
 *  audio_health frame. Tolerates unknown fields and absent optionals. */
export function parseHeartbeat(msg: unknown, receivedAt: number): ClientHeartbeat | null {
  if (!msg || typeof msg !== 'object') return null;
  const m = msg as Record<string, unknown>;
  if (m.t !== 'audio_health' || typeof m.n !== 'string') return null;
  const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
  const numOrNull = (v: unknown): number | null =>
    typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : null;
  const c = Array.isArray(m.c) ? (m.c as unknown[]) : [];
  const x = Array.isArray(m.x) ? (m.x as unknown[]) : [];
  const ba = Array.isArray(m.ba) ? (m.ba as unknown[]) : null;
  const og = Array.isArray(m.og) && m.og.length >= 2 ? (m.og as unknown[]) : null;
  const episodes: ClientEpisode[] = [];
  if (Array.isArray(m.ep)) {
    // Cap at 2× the client window: telemetry is bounded by contract, and an
    // oversized array must not become unbounded ingest work.
    for (const e of (m.ep as unknown[]).slice(0, 8)) {
      if (Array.isArray(e) && typeof e[0] === 'number' && typeof e[1] === 'string') {
        if (e[1] === 'g') {
          episodes.push({ id: e[0], kind: 'gap', startMs: num(e[2]), durationMs: num(e[3]) });
        } else if (e[1] === 's') {
          episodes.push({
            id: e[0],
            kind: 'speech',
            onsetSeq: num(e[2]),
            offsetSeq: num(e[3]),
            maxRmsPm: num(e[4]),
            aboveFloorMs: num(e[5]),
          });
        }
        // Unknown kinds are dropped, not misclassified — a future client's
        // new episode type must not masquerade as speech evidence.
      }
    }
  }
  return {
    nonce: m.n,
    seq: num(m.q),
    epochAgeMs: numOrNull(m.ea),
    capCallbacks: num(c[0]),
    bytesSent: num(c[1]),
    sendSkipped: num(c[2]),
    sendFailed: num(c[3]),
    chunksRecv: num(c[4]),
    chunksScheduled: num(c[5]),
    chunksEnded: num(c[6]),
    chunksCancelled: num(c[7]),
    ctxTimeMs: numOrNull(x[0]),
    scheduledDepth: numOrNull(x[1]),
    lastEndedAgoMs: numOrNull(x[2]),
    ctxState: typeof m.cs === 'string' ? m.cs : null,
    captureState: typeof m.cap === 'string' ? m.cap : null,
    ctxSuspendCount: num(m.sc),
    bufferedAmount: ba ? numOrNull(ba[0]) : null,
    bufferedHighWater: ba ? numOrNull(ba[1]) : null,
    muted: m.mu === 1,
    openGap: og ? { startMs: num(og[0]), ageMs: num(og[1]) } : null,
    episodeOverflow: num(m.eo),
    episodes,
    receivedAt,
  };
}

export function createAudioHealthLedger(opts: AudioHealthOptions): AudioHealthLedger {
  const now = opts.nowFn ?? Date.now;
  const log = opts.log ?? (() => {});
  const floor = opts.speechRmsFloor ?? ENGINE_SPEECH_RMS_FLOOR;
  const hangMs = opts.speechHangMs ?? ENGINE_SPEECH_HANG_MS;

  // ── epoch (engine-minted, Tranche A — round-4 #7) ──
  // Minted in onClientConnected (per the design — a connection that dies
  // before its first heartbeat still persists a real epoch); the client's
  // nonce binds to it on first heartbeat sight. A nonce change WITHOUT a
  // connect event (should not happen at the pin) re-mints defensively.
  let epoch: number | null = null;
  let nonce: string | null = null;
  let lastMintedEpoch = 0;
  /** Receipt-time approximation of the client's epoch start (first heartbeat
   *  fires on the client's first 500 ms stats tick) — lets the matrix place
   *  client-epoch-relative episode intervals on the engine clock, labeled
   *  approximate. */
  let epochStartApproxMs: number | null = null;
  let lastMatrixVerdict: string | null = null;
  let lastMatrixFacts: MatrixFactsRecord | null = null;
  let lastMatrixReasons: string[] | null = null;

  // ── lineage + context (design §1.1/§1.4; lineage OUTLIVES client epochs) ──
  let logicalSessionId = 0;
  let lineageState: 'none' | 'fresh' | 'resumed' | 'suspected-sever' = 'none';
  let lastAttemptHandleSupplied = false;
  let contextTokens: number | null = null;
  let contextTokensAt: number | null = null;
  let contextTokensDetails: Record<string, number> | null = null;
  // per-tick upstream rate window (30 s tick only — never the frame path)
  let prevUpTick: {
    generation: number | null;
    aQ: number;
    aW: number;
    vQ: number;
    vW: number;
    drop: number;
  } | null = null;

  function sampleDiagnostics(): SessionDiagnosticsSample | null {
    try {
      return opts.getSessionDiagnostics?.() ?? null;
    } catch {
      return null; // diagnostics failure must never break a tick
    }
  }

  // ── frame-path counters (session-layer coverage only) ──
  let deliveredFrames = 0;
  let deliveredBytes = 0;
  let lastDeliveredAt: number | null = null;
  let egressFrames = 0;
  let egressBytes = 0;
  let lastEgressAt: number | null = null;

  // ── ingress speech tracker (canonical) ──
  let ingressRms = 0;
  let speechEager = false;
  let onsetAt: number | null = null;
  let lastAboveFloorAt: number | null = null;
  /** Armed at speech onset, consumed by the next MODEL EVENT (D7.3 latency). */
  let pendingSpeechOnsetAt: number | null = null;
  let lastSpeechToModelMs: number | null = null;
  /** Any model activity: first audio out, turn start, tool call — whichever
   *  the engine observes first (D7.1: model EVENT, not model audio). */
  let lastModelEventAt: number | null = null;

  // ── client heartbeat mirror ──
  let lastHb: ClientHeartbeat | null = null;
  let prevHb: ClientHeartbeat | null = null;
  let heartbeatCount = 0;
  let seenEpisodeIds = new Set<number>();
  /** Gap episodes only — the [Health] `gaps` field must not count speech. */
  let gapEpisodeCount = 0;
  let newEpisodeIds: number[] = [];
  let sendSkippedGrew = false;
  const zeroTotals = () => ({
    capCallbacks: 0,
    bytesSent: 0,
    sendSkipped: 0,
    sendFailed: 0,
    chunksRecv: 0,
    chunksScheduled: 0,
    chunksEnded: 0,
    chunksCancelled: 0,
  });
  let clientTotals = zeroTotals();

  // ── persistence + latency ──
  let samplesSkipped = 0;
  let prevSamplesSkipped = 0;
  let lastTurnLatencyMs: number | null = null;

  // ── per-tick rate windows ([Health]) ──
  let prevTickAt = now();
  let prevDeliveredFrames = 0;
  let prevEgressFrames = 0;

  const wrapped: WeakSet<object> = new WeakSet();

  function resetEpochState(): void {
    epoch = null;
    nonce = null;
    epochStartApproxMs = null;
    lastMatrixVerdict = null;
    lastMatrixFacts = null;
    lastMatrixReasons = null;
    deliveredFrames = 0;
    deliveredBytes = 0;
    lastDeliveredAt = null;
    egressFrames = 0;
    egressBytes = 0;
    lastEgressAt = null;
    ingressRms = 0;
    speechEager = false;
    onsetAt = null;
    lastAboveFloorAt = null;
    lastHb = null;
    prevHb = null;
    heartbeatCount = 0;
    seenEpisodeIds = new Set();
    gapEpisodeCount = 0;
    newEpisodeIds = [];
    sendSkippedGrew = false;
    clientTotals = zeroTotals();
    prevDeliveredFrames = 0;
    prevEgressFrames = 0;
    prevTickAt = now();
    samplesSkipped = 0;
    prevSamplesSkipped = 0;
    lastTurnLatencyMs = null;
    pendingSpeechOnsetAt = null;
    lastSpeechToModelMs = null;
    lastModelEventAt = null;
  }

  function noteIngress(data: unknown): void {
    // §D7.0b FRAME PATH: O(1) writes + one BOUNDED subsampled pass. `data`
    // is the int16le PCM Buffer bodhi hands to handleAudioFromClient — but
    // its length is client-controlled, so the pass caps at 8 KiB (≥ the
    // nominal 1364 B frame; a jumbo frame's tail adds nothing to a level
    // estimate).
    const t = now();
    deliveredFrames++;
    const buf = data as { length?: number; readInt16LE?: (o: number) => number };
    const len = typeof buf?.length === 'number' ? buf.length : 0;
    deliveredBytes += len;
    lastDeliveredAt = t;
    if (len >= 2 && typeof buf.readInt16LE === 'function') {
      const scanLen = Math.min(len, 8192);
      // Every 4th sample (= every 8 bytes), allocation-free.
      let sum = 0;
      let n = 0;
      for (let o = 0; o + 1 < scanLen; o += 8) {
        const v = buf.readInt16LE(o) / 32768;
        sum += v * v;
        n++;
      }
      const rms = n > 0 ? Math.sqrt(sum / n) : 0;
      ingressRms = rms;
      // Decay FIRST: after a silent stall longer than the hangover, the next
      // above-floor frame is a NEW onset — without this, onsetAt would stay
      // latched to the first utterance forever (codex round-2 #3).
      if (speechEager && lastAboveFloorAt !== null && t - lastAboveFloorAt > hangMs) {
        speechEager = false;
      }
      if (rms >= floor) {
        if (!speechEager) {
          speechEager = true;
          onsetAt = t;
          // Arm the speech→first-model-event latency sample (D7.3: the half
          // FE-1 actually inflated), consumed by the next egress frame.
          pendingSpeechOnsetAt = t;
        }
        lastAboveFloorAt = t;
      }
    }
  }

  function noteModelEventAt(t: number): void {
    lastModelEventAt = t;
    if (pendingSpeechOnsetAt !== null) {
      // Server-clock ingress-onset → FIRST model event (audio, turn start,
      // or tool call — whichever lands first; receipt-time approximation).
      lastSpeechToModelMs = t - pendingSpeechOnsetAt;
      pendingSpeechOnsetAt = null;
    }
  }

  function noteEgress(base64: unknown): void {
    // §D7.0b FRAME PATH: O(1). `base64` is the outbound audio string.
    egressFrames++;
    const s = base64 as { length?: number };
    if (typeof s?.length === 'number') egressBytes += Math.floor((s.length * 3) / 4);
    lastEgressAt = now();
    noteModelEventAt(lastEgressAt);
  }

  function mintEpoch(): number {
    lastMintedEpoch = Math.max(now(), lastMintedEpoch + 1);
    return lastMintedEpoch;
  }

  function ingestHeartbeat(msg: unknown): void {
    const hb = parseHeartbeat(msg, now());
    if (!hb) return;
    if (hb.nonce !== nonce) {
      if (nonce === null && epoch !== null) {
        // First heartbeat of the connection: bind the client nonce to the
        // epoch minted at connect.
        log(`[AudioHealth] client nonce ${hb.nonce} bound to epoch ${epoch}`);
        nonce = hb.nonce;
      } else {
        // Nonce changed without a connect event (or a heartbeat arrived
        // before any connect) — a FULL epoch boundary, not just a client
        // baseline reset: server counters, heartbeats, latency samples, and
        // tick windows from the old epoch must not bleed into the new one.
        resetEpochState();
        epoch = mintEpoch();
        nonce = hb.nonce;
        log(`[AudioHealth] epoch ${epoch} minted for unexpected client nonce ${hb.nonce}`);
      }
    } else {
      prevHb = lastHb;
    }
    // Epoch placement: the client stamps its own epoch age into every beat —
    // receivedAt − ea places the client's epoch-relative intervals on the
    // engine clock (accurate to network latency). Refreshed per beat; the
    // pre-`ea` fallback stays a coarse first-beat guess.
    epochStartApproxMs =
      hb.epochAgeMs !== null
        ? hb.receivedAt - hb.epochAgeMs
        : (epochStartApproxMs ?? hb.receivedAt - 500);
    for (const e of hb.episodes) {
      if (!seenEpisodeIds.has(e.id)) {
        seenEpisodeIds.add(e.id);
        newEpisodeIds.push(e.id);
        if (e.kind === 'gap') gapEpisodeCount++;
        if (seenEpisodeIds.size > 128) seenEpisodeIds.clear(); // bounded (ids re-add harmlessly)
      }
    }
    // Counter fields are deltas: accumulate into the per-epoch totals.
    clientTotals.capCallbacks += hb.capCallbacks;
    clientTotals.bytesSent += hb.bytesSent;
    clientTotals.sendSkipped += hb.sendSkipped;
    clientTotals.sendFailed += hb.sendFailed;
    clientTotals.chunksRecv += hb.chunksRecv;
    clientTotals.chunksScheduled += hb.chunksScheduled;
    clientTotals.chunksEnded += hb.chunksEnded;
    clientTotals.chunksCancelled += hb.chunksCancelled;
    if (hb.sendSkipped > 0) sendSkippedGrew = true;
    lastHb = hb;
    heartbeatCount++;
  }

  function speechEvidence(): SpeechEvidence {
    const t = now();
    // Lazy decay: a stalled client stops delivering frames, so the eager
    // flag alone would latch active forever — the hangover check here makes
    // silence (and stalls) end the evidence.
    const active = lastAboveFloorAt !== null && t - lastAboveFloorAt <= hangMs;
    return { active, onsetAt: active ? onsetAt : null, lastOnsetAt: onsetAt, lastAboveFloorAt };
  }

  function inputHealth(
    clientConnected: boolean,
  ): 'ok' | 'degraded' | 'stalled' | 'no-client' | 'unknown' {
    if (!clientConnected) return 'no-client';
    const t = now();
    const hbFresh = lastHb !== null && t - lastHb.receivedAt <= HEARTBEAT_STALE_MS;
    const muted = lastHb?.muted ?? false;
    // A degraded capture (client recovery exhausted) is worth surfacing even
    // muted — the input will still be dead on unmute.
    if (hbFresh && lastHb?.captureState === 'd') return 'degraded';
    // Mute is a user choice, not an input failure — nothing below can
    // conclude 'stalled' while muted.
    if (muted && hbFresh) return 'ok';
    // Client-reported evidence counts only while its heartbeat is FRESH — a
    // detached client's retained open gap must not report stalled forever.
    if (hbFresh && lastHb) {
      if (lastHb.openGap) return 'stalled';
      if (lastHb.ctxState === 's' || lastHb.ctxState === 'c') return 'stalled';
    }
    if (lastDeliveredAt !== null && t - lastDeliveredAt > INGRESS_STALL_MS) return 'stalled';
    if (lastDeliveredAt === null) {
      // Heartbeats crossing while NO PCM has ever reached the session is a
      // stall, not health (codex round-2 #4).
      return heartbeatCount > 0 ? 'stalled' : 'unknown';
    }
    return 'ok';
  }

  return {
    wrapSession(session: unknown): void {
      const s = session as Record<string, unknown> & object;
      if (wrapped.has(s)) return;
      wrapped.add(s);
      const origAudio = (s.handleAudioFromClient as ((d: unknown) => void) | undefined)?.bind(s);
      if (origAudio) {
        s.handleAudioFromClient = (data: unknown) => {
          noteIngress(data);
          origAudio(data);
        };
      }
      const origJson = (s.handleJsonFromClient as ((m: unknown) => void) | undefined)?.bind(s);
      if (origJson) {
        s.handleJsonFromClient = (message: unknown) => {
          const m = message as { t?: unknown } | null;
          if (m && m.t === 'audio_health') {
            // Intercepted: health telemetry is engine-consumed, never routed
            // into bodhi's client-message handling.
            try {
              ingestHeartbeat(message);
            } catch {
              /* a malformed frame must never break the message path */
            }
            return;
          }
          origJson(message);
        };
      }
      const origOut = (s.handleAudioOutput as ((d: unknown) => void) | undefined)?.bind(s);
      if (origOut) {
        s.handleAudioOutput = (data: unknown) => {
          noteEgress(data);
          origOut(data);
        };
      }
    },

    onClientConnected(): void {
      resetEpochState();
      // The design mints the epoch HERE: a connection that dies before its
      // first heartbeat still persists rows under a real epoch. The client's
      // nonce binds on first heartbeat sight.
      epoch = mintEpoch();
      log(`[AudioHealth] epoch ${epoch} minted (client connected; nonce pending)`);
    },

    onClientDisconnected(): void {
      // Keep the epoch's evidence for the final persist; eager speech ends.
      speechEager = false;
    },

    noteTurnLatency(totalE2EMs: number | undefined): void {
      if (typeof totalE2EMs === 'number' && Number.isFinite(totalE2EMs)) {
        lastTurnLatencyMs = totalE2EMs;
      }
    },

    noteModelEvent(): void {
      noteModelEventAt(now());
    },

    getSpeechEvidence: speechEvidence,
    getInputHealth: inputHealth,

    getSnapshot(clientConnected: boolean): AudioHealthSnapshot {
      const diag = sampleDiagnostics();
      return {
        coverage: 'session-only',
        upstream: diag?.upstream ?? null,
        transportGeneration: diag?.transportGeneration ?? null,
        // NOT `?? 0`: a null diag means UNOBSERVED, and a zero here becomes a
        // lifetime total masquerading as a one-window delta on the next tick.
        echoSuppressed: diag?.echoSuppressed ?? null,
        logicalSessionId,
        lineageState,
        contextTokens,
        contextTokensAt,
        contextTokensDetails: contextTokensDetails ? { ...contextTokensDetails } : null,
        epoch,
        nonce,
        deliveredFrames,
        deliveredBytes,
        lastDeliveredAt,
        egressFrames,
        egressBytes,
        lastEgressAt,
        speech: speechEvidence(),
        ingressRms,
        lastHeartbeat: lastHb,
        clientTotals: { ...clientTotals },
        heartbeatCount,
        newEpisodeIds: [...newEpisodeIds],
        samplesSkipped,
        lastTurnLatencyMs,
        lastSpeechToModelMs,
        lastModelEventAt,
        inputHealth: inputHealth(clientConnected),
        epochStartApproxMs,
        lastMatrixVerdict,
        lastMatrixFacts: lastMatrixFacts ? { ...lastMatrixFacts } : null,
        lastMatrixReasons: lastMatrixReasons ? [...lastMatrixReasons] : null,
      };
    },

    noteMatrixVerdict(verdict: string, facts?: MatrixFactsRecord, reasons?: string[]): void {
      lastMatrixVerdict = verdict;
      lastMatrixFacts = facts ?? null;
      lastMatrixReasons = reasons ?? null;
    },

    noteUsageMetadata(
      promptTokenCount: number | null | undefined,
      promptTokensDetails?: ReadonlyArray<{ modality?: string; tokenCount?: number }> | null,
    ): void {
      if (typeof promptTokenCount === 'number' && Number.isFinite(promptTokenCount)) {
        contextTokens = promptTokenCount;
        contextTokensAt = now();
      }
      if (Array.isArray(promptTokensDetails)) {
        const details: Record<string, number> = {};
        for (const d of promptTokensDetails) {
          if (d && typeof d.modality === 'string' && typeof d.tokenCount === 'number') {
            details[d.modality] = (details[d.modality] ?? 0) + d.tokenCount;
          }
        }
        if (Object.keys(details).length > 0) contextTokensDetails = details;
      }
    },

    noteLifecycleEvent(event: ConnectionLifecycleEvent): void {
      // Design §1.1, sutando-side: mint on a handle-less setup-ok; a resumed
      // setup keeps the lineage; suspected-sever is TERMINAL (nothing
      // observable can promote it — the promotion signal does not exist).
      if (event.kind === 'attempt') {
        lastAttemptHandleSupplied = event.handleSupplied;
        return;
      }
      let committed = false;
      let generation: number | null = null;
      if (event.kind === 'setup-ok') {
        generation = event.transportGeneration;
        if (!lastAttemptHandleSupplied) {
          logicalSessionId++;
          lineageState = 'fresh';
          // Context occupancy belongs to the lineage — a fresh one starts unknown.
          contextTokens = null;
          contextTokensAt = null;
          contextTokensDetails = null;
        } else if (lineageState !== 'suspected-sever') {
          lineageState = 'resumed';
        }
        committed = true; // every successful setup commits the attempt to a lineage
      } else if (event.kind === 'setup-failed' && lastAttemptHandleSupplied) {
        lineageState = 'suspected-sever';
        committed = true;
      } else if (
        event.kind === 'generation-close' &&
        lastAttemptHandleSupplied &&
        event.code === 1008
      ) {
        generation = event.transportGeneration;
        lineageState = 'suspected-sever';
        committed = true;
      }
      if (!committed) return;
      // §1.1: a lineage can be re-labelled after rows are already persisted —
      // emit a generation→lineage reconciliation record (attempt, generation,
      // resolved lineage, final state) so offline analysis re-attributes
      // earlier rows instead of trusting a stale in-row id. Connection-rate,
      // never the frame path; a busy mailbox skips it like any sample.
      log(
        `[AudioHealth] lineage ${lineageState} id=${logicalSessionId} ` +
          `attempt=${event.connectAttemptId} gen=${generation ?? 'n/a'}`,
      );
      if (opts.persist) {
        const ok = opts.persist({
          tsUnix: Math.floor(now() / 1000),
          sessionId: opts.sessionId,
          epoch,
          nonce,
          reason: 'lineage',
          payload: JSON.stringify({
            connectAttemptId: event.connectAttemptId,
            transportGeneration: generation,
            logicalSessionId,
            lineageState,
          }),
        });
        if (!ok) samplesSkipped++;
      }
    },

    healthSegments(clientConnected: boolean, serverBufferedAmount?: number | null): string {
      const t = now();
      const dt = Math.max(0.001, (t - prevTickAt) / 1000);
      const inFps = (deliveredFrames - prevDeliveredFrames) / dt;
      const outFps = (egressFrames - prevEgressFrames) / dt;
      prevTickAt = t;
      prevDeliveredFrames = deliveredFrames;
      prevEgressFrames = egressFrames;
      const ago = (v: number | null): string => (v === null ? 'n/a' : ((t - v) / 1000).toFixed(1) + 's');
      const audioIn = `audioIn={fps:${inFps.toFixed(1)},lastAgo:${ago(lastDeliveredAt)},coverage:session-only}`;
      // The bodhi pin now exposes what the agent handed the SDK. Rates are
      // per-tick deltas; a generation change resets the counters, so that tick
      // renders from zero rather than a negative delta. buf stays n/a (B6).
      let up = 'up=n/a';
      const diag = sampleDiagnostics();
      if (diag?.upstream) {
        const a = diag.upstream.audio;
        const v = diag.upstream.video;
        const cur = {
          generation: diag.transportGeneration,
          aQ: a.queued,
          aW: a.queuedWireBytesEstimate,
          vQ: v.queued,
          vW: v.queuedWireBytesEstimate,
          drop:
            a.skippedNoSession + a.threw + v.skippedNoSession + v.threw + v.unsupportedMime,
        };
        if (prevUpTick === null) {
          // First observed tick, or the tick after a missed sample: no
          // adjacent baseline exists, and cumulative totals over one dt
          // would fabricate a rate. Baseline now; rates start next tick.
          up = 'up=n/a(baselining)';
        } else {
          // A generation change WITH an adjacent observed tick renders from
          // zero — the new generation's counters span at most this window.
          const base =
            prevUpTick.generation === cur.generation
              ? prevUpTick
              : { aQ: 0, aW: 0, vQ: 0, vW: 0, drop: 0 };
          // Safe by the pin's construction: bodhi resets these counters and
          // bumps transportGeneration on adjacent lines (0d506ead :854-855).
          const d = (x: number, y: number) => Math.max(0, x - y);
          up =
            `up={aQ/s:${(d(cur.aQ, base.aQ) / dt).toFixed(1)},` +
            `aKB/s:${(d(cur.aW, base.aW) / dt / 1024).toFixed(0)}w,` +
            `vQ/s:${(d(cur.vQ, base.vQ) / dt).toFixed(1)},` +
            `vKB/s:${(d(cur.vW, base.vW) / dt / 1024).toFixed(0)}w,` +
            `drop:${d(cur.drop, base.drop)},buf:n/a}`;
        }
        prevUpTick = cur;
      } else {
        prevUpTick = null;
      }
      const ctx =
        contextTokens !== null && contextTokensAt !== null
          ? ` ctx={tok:${contextTokens},age:${((t - contextTokensAt) / 1000).toFixed(1)}s}`
          : '';
      const endedLag =
        lastHb?.lastEndedAgoMs != null ? (lastHb.lastEndedAgoMs / 1000).toFixed(1) + 's' : 'n/a';
      // out.buffered is the SERVER's egress socket toward the client; the
      // client's own uplink bufferedAmount lives under clientHealth.upBuf.
      const out = `out={fps:${outFps.toFixed(1)},buffered:${serverBufferedAmount ?? 'n/a'},endedLag:${endedLag}}`;
      let client = 'clientHealth=n/a';
      if (lastHb) {
        const hbAge = ((t - lastHb.receivedAt) / 1000).toFixed(1) + 's';
        let capRate = 'n/a';
        if (prevHb && lastHb.receivedAt > prevHb.receivedAt) {
          // Counter fields are deltas — the rate is delta over inter-beat time.
          capRate = ((lastHb.capCallbacks * 1000) / (lastHb.receivedAt - prevHb.receivedAt)).toFixed(1);
        }
        // rms is the ENGINE ingress tracker (canonical evidence) — the
        // client's own rms travels only as latched episode intervals.
        client =
          `clientHealth={age:${hbAge},capCb/s:${capRate},rms:${ingressRms.toFixed(3)},` +
          `ctx:${lastHb.ctxState ?? '?'},cap:${lastHb.captureState ?? '?'},gaps:${gapEpisodeCount},` +
          `upBuf:${lastHb.bufferedAmount ?? 'n/a'},skip:${clientTotals.sendSkipped}` +
          `${lastHb.muted ? ',muted' : ''}${lastHb.openGap ? ',STALLED' : ''}}`;
      }
      const lat = lastTurnLatencyMs != null ? ` latency=${lastTurnLatencyMs}ms` : '';
      const s2m = lastSpeechToModelMs != null ? ` speech2model=${lastSpeechToModelMs}ms` : '';
      const ep = epoch != null ? ` epoch=${epoch}` : '';
      const health = clientConnected ? inputHealth(true) : 'no-client';
      return `${audioIn} ${up} ${out} ${client}${ep}${lat}${s2m}${ctx} inputHealth=${health}`;
    },

    anomalies(clientConnected: boolean): { anomalous: boolean; reasons: string[] } {
      const t = now();
      const reasons: string[] = [];
      const hbFresh = lastHb !== null && t - lastHb.receivedAt <= HEARTBEAT_STALE_MS;
      const muted = lastHb?.muted ?? false;
      // Client-reported stall evidence only counts while attached, unmuted,
      // and FRESH — a detached client's retained open gap is not a live
      // anomaly (codex round-2 #4).
      if (clientConnected && !muted && hbFresh && lastHb?.openGap) reasons.push('capStalled');
      // Episode evidence still latches into the snapshot regardless, but a
      // detached client re-sending its window is not a LIVE anomaly.
      if (clientConnected && newEpisodeIds.length > 0) reasons.push(`episodes:${newEpisodeIds.length}`);
      if (clientConnected && lastHb && !hbFresh) reasons.push('heartbeat-stale');
      if (
        clientConnected &&
        !muted &&
        lastDeliveredAt !== null &&
        t - lastDeliveredAt > INGRESS_STALL_MS
      ) {
        reasons.push('ingress-stalled');
      }
      if (sendSkippedGrew) reasons.push('sendSkipped');
      if (samplesSkipped > prevSamplesSkipped) reasons.push('persistSkipped');
      return { anomalous: reasons.length > 0, reasons };
    },

    clearTickLatches(): void {
      newEpisodeIds = [];
      sendSkippedGrew = false;
      prevSamplesSkipped = samplesSkipped;
    },

    persistTick(reason: 'timer' | 'anomaly' | 'final', clientConnected: boolean): void {
      if (!opts.persist) return;
      const snapshot = this.getSnapshot(clientConnected);
      let payload = JSON.stringify(snapshot);
      if (Buffer.byteLength(payload) > PERSIST_PAYLOAD_MAX_BYTES) {
        // Never slice a JSON document mid-string: over budget, persist a
        // reduced-but-VALID row. Design §1.6: the reduced row must still be
        // REPLAYABLE — so it keeps every matrix-evaluator input SNAPSHOT-
        // SHAPED (option 1: same names, same nesting, so a replay feeds it
        // to evaluateMatrix directly) plus the recorded decision (option 2);
        // what it drops is the prose/latency extras the evaluator never reads.
        const reduced: Record<string, unknown> = {
          truncated: true,
          coverage: snapshot.coverage,
          epoch: snapshot.epoch,
          nonce: snapshot.nonce,
          inputHealth: snapshot.inputHealth,
          samplesSkipped: snapshot.samplesSkipped,
          upstream: snapshot.upstream,
          transportGeneration: snapshot.transportGeneration,
          echoSuppressed: snapshot.echoSuppressed,
          logicalSessionId: snapshot.logicalSessionId,
          lineageState: snapshot.lineageState,
          contextTokens: snapshot.contextTokens,
          contextTokensAt: snapshot.contextTokensAt,
          contextTokensDetails: snapshot.contextTokensDetails,
          deliveredFrames: snapshot.deliveredFrames,
          lastDeliveredAt: snapshot.lastDeliveredAt,
          egressFrames: snapshot.egressFrames,
          speech: snapshot.speech,
          lastHeartbeat: snapshot.lastHeartbeat,
          clientTotals: snapshot.clientTotals,
          epochStartApproxMs: snapshot.epochStartApproxMs,
          lastModelEventAt: snapshot.lastModelEventAt,
          lastMatrixVerdict: snapshot.lastMatrixVerdict,
          lastMatrixFacts: snapshot.lastMatrixFacts,
          lastMatrixReasons: snapshot.lastMatrixReasons,
        };
        payload = JSON.stringify(reduced);
        if (Buffer.byteLength(payload) > PERSIST_PAYLOAD_MAX_BYTES && snapshot.lastHeartbeat) {
          // Still over: the episode list is the only unbounded-ish input.
          // Strip it LAST and say so — the recorded decision still rides.
          reduced.lastHeartbeat = { ...snapshot.lastHeartbeat, episodes: [] };
          reduced.episodesStripped = true;
          payload = JSON.stringify(reduced);
        }
      }
      const row: HealthRow = {
        tsUnix: Math.floor(now() / 1000),
        sessionId: opts.sessionId,
        epoch,
        nonce,
        reason,
        payload,
      };
      if (!opts.persist(row)) {
        // One-slot mailbox busy (or worker down): the sample is SKIPPED, not
        // queued — lag is visible in the ledger, never felt in audio.
        samplesSkipped++;
      }
    },

    ingestHeartbeat,
  };
}
