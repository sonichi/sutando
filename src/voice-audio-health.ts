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
  episodes: Array<{ id: number; kind: string }>;
  receivedAt: number;
}

/** One persistence row (the injected mailbox writes it to sqlite). */
export interface HealthRow {
  tsUnix: number;
  sessionId: string;
  epoch: number | null;
  nonce: string | null;
  reason: 'timer' | 'anomaly' | 'final';
  payload: string;
}

export interface AudioHealthSnapshot {
  coverage: 'session-only';
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
  inputHealth: 'ok' | 'stalled' | 'no-client' | 'unknown';
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
  /** Canonical speech evidence — the vision gate and the matrix consume
   *  THIS (precedence: ingress tracker > client RMS intervals). */
  getSpeechEvidence(): SpeechEvidence;
  getInputHealth(clientConnected: boolean): 'ok' | 'stalled' | 'no-client' | 'unknown';
  getSnapshot(clientConnected: boolean): AudioHealthSnapshot;
  /** Upgraded [Health] segments (30 s tick). Also advances the per-tick
   *  rate windows. */
  healthSegments(clientConnected: boolean): string;
  /** Anomaly-overrides-suppression (D7.1): true forces a [Health] line even
   *  during an unchanged-ACTIVE streak. Clears the per-tick latches. */
  anomalySinceLastTick(clientConnected: boolean): { anomalous: boolean; reasons: string[] };
  /** Try-enqueue one row into the persistence mailbox; a busy mailbox skips
   *  the sample (samplesSkipped), never queues (§D7.0b). */
  persistTick(reason: 'timer' | 'anomaly' | 'final', clientConnected: boolean): void;
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
  const episodes: Array<{ id: number; kind: string }> = [];
  if (Array.isArray(m.ep)) {
    for (const e of m.ep as unknown[]) {
      if (Array.isArray(e) && typeof e[0] === 'number' && typeof e[1] === 'string') {
        episodes.push({ id: e[0], kind: e[1] === 'g' ? 'gap' : 'speech' });
      }
    }
  }
  return {
    nonce: m.n,
    seq: num(m.q),
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
  let epoch: number | null = null;
  let nonce: string | null = null;
  let lastMintedEpoch = 0;
  const nonceToEpoch = new Map<string, number>();

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

  // ── client heartbeat mirror ──
  let lastHb: ClientHeartbeat | null = null;
  let prevHb: ClientHeartbeat | null = null;
  let heartbeatCount = 0;
  let seenEpisodeIds = new Set<number>();
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
    newEpisodeIds = [];
    sendSkippedGrew = false;
    clientTotals = zeroTotals();
    prevDeliveredFrames = 0;
    prevEgressFrames = 0;
  }

  function noteIngress(data: unknown): void {
    // §D7.0b FRAME PATH: O(1) writes + one subsampled pass. `data` is the
    // int16le PCM Buffer bodhi hands to handleAudioFromClient.
    const t = now();
    deliveredFrames++;
    const buf = data as { length?: number; readInt16LE?: (o: number) => number };
    const len = typeof buf?.length === 'number' ? buf.length : 0;
    deliveredBytes += len;
    lastDeliveredAt = t;
    if (len >= 2 && typeof buf.readInt16LE === 'function') {
      // Every 4th sample (= every 8 bytes), allocation-free.
      let sum = 0;
      let n = 0;
      for (let o = 0; o + 1 < len; o += 8) {
        const v = buf.readInt16LE(o) / 32768;
        sum += v * v;
        n++;
      }
      const rms = n > 0 ? Math.sqrt(sum / n) : 0;
      ingressRms = rms;
      if (rms >= floor) {
        if (!speechEager) {
          speechEager = true;
          onsetAt = t;
        }
        lastAboveFloorAt = t;
      } else if (speechEager && lastAboveFloorAt !== null && t - lastAboveFloorAt > hangMs) {
        speechEager = false;
      }
    }
  }

  function noteEgress(base64: unknown): void {
    // §D7.0b FRAME PATH: O(1). `base64` is the outbound audio string.
    egressFrames++;
    const s = base64 as { length?: number };
    if (typeof s?.length === 'number') egressBytes += Math.floor((s.length * 3) / 4);
    lastEgressAt = now();
  }

  function ingestHeartbeat(msg: unknown): void {
    const hb = parseHeartbeat(msg, now());
    if (!hb) return;
    if (hb.nonce !== nonce) {
      // First sight of a new client connection: mint the engine-owned epoch
      // (monotonic, collision-free) and key everything to it.
      const existing = nonceToEpoch.get(hb.nonce);
      if (existing !== undefined) {
        epoch = existing;
      } else {
        lastMintedEpoch = Math.max(now(), lastMintedEpoch + 1);
        epoch = lastMintedEpoch;
        nonceToEpoch.set(hb.nonce, epoch);
        if (nonceToEpoch.size > 4) {
          const oldest = nonceToEpoch.keys().next().value;
          if (oldest !== undefined) nonceToEpoch.delete(oldest);
        }
        log(`[AudioHealth] epoch ${epoch} minted for client nonce ${hb.nonce}`);
      }
      nonce = hb.nonce;
      prevHb = null;
      seenEpisodeIds = new Set();
      newEpisodeIds = [];
      clientTotals = zeroTotals();
    } else {
      prevHb = lastHb;
    }
    for (const e of hb.episodes) {
      if (!seenEpisodeIds.has(e.id)) {
        seenEpisodeIds.add(e.id);
        newEpisodeIds.push(e.id);
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
    return { active, onsetAt: active ? onsetAt : null, lastAboveFloorAt };
  }

  function inputHealth(clientConnected: boolean): 'ok' | 'stalled' | 'no-client' | 'unknown' {
    if (!clientConnected) return 'no-client';
    const t = now();
    if (lastHb?.openGap) return 'stalled';
    const muted = lastHb?.muted ?? false;
    if (!muted && lastDeliveredAt !== null && t - lastDeliveredAt > INGRESS_STALL_MS) return 'stalled';
    if (lastDeliveredAt === null && heartbeatCount === 0) return 'unknown';
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

    getSpeechEvidence: speechEvidence,
    getInputHealth: inputHealth,

    getSnapshot(clientConnected: boolean): AudioHealthSnapshot {
      return {
        coverage: 'session-only',
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
        inputHealth: inputHealth(clientConnected),
      };
    },

    healthSegments(clientConnected: boolean): string {
      const t = now();
      const dt = Math.max(0.001, (t - prevTickAt) / 1000);
      const inFps = (deliveredFrames - prevDeliveredFrames) / dt;
      const outFps = (egressFrames - prevEgressFrames) / dt;
      prevTickAt = t;
      prevDeliveredFrames = deliveredFrames;
      prevEgressFrames = egressFrames;
      const ago = (v: number | null): string => (v === null ? 'n/a' : ((t - v) / 1000).toFixed(1) + 's');
      const audioIn = `audioIn={fps:${inFps.toFixed(1)},lastAgo:${ago(lastDeliveredAt)},coverage:session-only}`;
      // Tranche A cannot see SDK accepts/discards (bodhi-internal) — up is
      // honestly n/a until the native counters land ([B] step 6).
      const up = 'up=n/a';
      const endedLag =
        lastHb?.lastEndedAgoMs != null ? (lastHb.lastEndedAgoMs / 1000).toFixed(1) + 's' : 'n/a';
      const out = `out={fps:${outFps.toFixed(1)},buffered:${lastHb?.bufferedAmount ?? 'n/a'},endedLag:${endedLag}}`;
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
          `ctx:${lastHb.ctxState ?? '?'},cap:${lastHb.captureState ?? '?'},gaps:${seenEpisodeIds.size},` +
          `skip:${clientTotals.sendSkipped}${lastHb.muted ? ',muted' : ''}${lastHb.openGap ? ',STALLED' : ''}}`;
      }
      const lat = lastTurnLatencyMs != null ? ` latency=${lastTurnLatencyMs}ms` : '';
      const ep = epoch != null ? ` epoch=${epoch}` : '';
      const health = clientConnected ? inputHealth(true) : 'no-client';
      return `${audioIn} ${up} ${out} ${client}${ep}${lat} inputHealth=${health}`;
    },

    anomalySinceLastTick(clientConnected: boolean): { anomalous: boolean; reasons: string[] } {
      const t = now();
      const reasons: string[] = [];
      if (lastHb?.openGap) reasons.push('capStalled');
      if (newEpisodeIds.length > 0) reasons.push(`episodes:${newEpisodeIds.length}`);
      if (clientConnected && lastHb && t - lastHb.receivedAt > HEARTBEAT_STALE_MS) {
        reasons.push('heartbeat-stale');
      }
      if (
        clientConnected &&
        !(lastHb?.muted ?? false) &&
        lastDeliveredAt !== null &&
        t - lastDeliveredAt > INGRESS_STALL_MS
      ) {
        reasons.push('ingress-stalled');
      }
      if (sendSkippedGrew) reasons.push('sendSkipped');
      if (samplesSkipped > prevSamplesSkipped) reasons.push('persistSkipped');
      // Clear the per-tick latches (counters themselves stay monotonic).
      newEpisodeIds = [];
      sendSkippedGrew = false;
      prevSamplesSkipped = samplesSkipped;
      return { anomalous: reasons.length > 0, reasons };
    },

    persistTick(reason: 'timer' | 'anomaly' | 'final', clientConnected: boolean): void {
      if (!opts.persist) return;
      const snapshot = this.getSnapshot(clientConnected);
      let payload = JSON.stringify(snapshot);
      if (payload.length > PERSIST_PAYLOAD_MAX_BYTES) payload = payload.slice(0, PERSIST_PAYLOAD_MAX_BYTES);
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
