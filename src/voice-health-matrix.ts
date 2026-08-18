// voice-health-matrix — P7 D7.2: the failure-localization matrix. A pure
// function over the engine ledger snapshot with HONEST outcomes: a partial
// ledger (coverage 'session-only', missing heartbeats, a reconnect window)
// can never produce a confident verdict about the layers the partiality
// obscures. Rows 2–4 (native server dispositions) therefore stay unreachable
// until the bodhi tranche flips coverage to 'full'; their patterns surface as
// `insufficient-evidence` with the blind hop named. Evaluated per 30 s health
// tick; the caller logs the verdict and notes it into the ledger for
// persistence.

import type { AudioHealthSnapshot } from './voice-audio-health.js';

export type MatrixVerdict =
  | 'reconnect-window'
  | 'insufficient-evidence'
  | 'client-capture-dead' // row 1
  | 'server-accounting-inconsistency' // row 2 [needs native counters]
  | 'server-discarding' // row 3 [needs native counters]
  | 'upstream-send-dead' // row 4 [needs native counters]
  | 'model-silent' // row 5 (the honest F4 residue)
  | 'client-send-backpressure' // row 6
  | 'egress-backpressure' // row 7a
  | 'client-playback-failure' // row 7b
  | 'healthy-idle'; // row 5′ + default

/** Counter values the next tick diffs against (returned by every evaluation). */
export interface MatrixBaseline {
  at: number;
  /** The epoch the counters belong to — deltas NEVER cross epochs (a
   *  reconnect resets every counter; a cross-epoch diff would be negative
   *  garbage that still pattern-matches). */
  epoch: number | null;
  deliveredFrames: number;
  capCallbacks: number;
  bytesSent: number;
  sendSkipped: number;
  chunksRecv: number;
  chunksScheduled: number;
  chunksEnded: number;
  chunksCancelled: number;
  ctxTimeMs: number | null;
  bufferedAmount: number | null;
  serverBufferedAmount: number | null;
  maxEpisodeId: number;
}

export interface MatrixInput {
  /** bodhi sessionManager.state (RECONNECTING/CONNECTING ⇒ row 0). */
  sessionState: string;
  clientConnected: boolean;
  snapshot: AudioHealthSnapshot;
  /** Previous tick's baseline (null on the first evaluation). */
  prev: MatrixBaseline | null;
  /** Server-side ws bufferedAmount toward the client (row 7a); absent ⇒ that
   *  pattern is insufficient-evidence. */
  serverBufferedAmount?: number | null;
  /** Last model event timestamp (row 5); absent ⇒ unknown. */
  lastModelEventAt?: number | null;
  now: number;
}

/** Raw, verdict-independent facts for the ACTIVE-silence watchdog (design:
 *  design-voice-active-silence-recovery.md §Trigger (a)). Identical under
 *  every coverage mode; fail closed when their inputs are missing.
 *  Formulas: ingressAdvanced = delta(deliveredFrames) > 0 at session-only
 *  coverage (the ingest-accepted delta once full coverage exists);
 *  modelSilentFor15s = lastModelEventAt !== null && now - lastModelEventAt
 *  > 15_000 (null is a fresh session, not silence). */
export interface MatrixFacts {
  /** Same-epoch baseline + heartbeat existed this tick; false ⇒ every other
   *  fact is false. */
  factsAvailable: boolean;
  speechInWindow: boolean;
  /** Monotonic timestamp of the retained speech observation's first
   *  above-floor sample; non-null exactly when speechInWindow. */
  speechObservedAt: number | null;
  ingressAdvanced: boolean;
  modelSilentFor15s: boolean;
}

const FACTS_UNAVAILABLE: MatrixFacts = Object.freeze({
  factsAvailable: false,
  speechInWindow: false,
  speechObservedAt: null,
  ingressAdvanced: false,
  modelSilentFor15s: false,
});

export interface MatrixResult {
  verdict: MatrixVerdict;
  reasons: string[];
  baseline: MatrixBaseline;
  facts: MatrixFacts;
}

/** An episode (or open gap) is only row-1 evidence while fresh. */
export const EPISODE_UNEXPIRED_MS = 90_000;

/** Ingress considered stalled for matrix purposes past this. */
export const MATRIX_INGRESS_STALL_MS = 5_000;

/** Minimum playback chunks scheduled in a window before row 7b can fire
 *  (turn boundaries legitimately schedule little). */
export const PLAYBACK_MIN_CHUNKS = 5;

function makeBaseline(input: MatrixInput): MatrixBaseline {
  const s = input.snapshot;
  const hb = s.lastHeartbeat;
  const sameEpoch = input.prev?.epoch === s.epoch;
  let maxEpisodeId = sameEpoch ? (input.prev?.maxEpisodeId ?? 0) : 0;
  for (const e of hb?.episodes ?? []) if (e.id > maxEpisodeId) maxEpisodeId = e.id;
  return {
    at: input.now,
    epoch: s.epoch,
    deliveredFrames: s.deliveredFrames,
    capCallbacks: s.clientTotals.capCallbacks,
    bytesSent: s.clientTotals.bytesSent,
    sendSkipped: s.clientTotals.sendSkipped,
    chunksRecv: s.clientTotals.chunksRecv,
    chunksScheduled: s.clientTotals.chunksScheduled,
    chunksEnded: s.clientTotals.chunksEnded,
    chunksCancelled: s.clientTotals.chunksCancelled,
    ctxTimeMs: hb?.ctxTimeMs ?? (sameEpoch ? (input.prev?.ctxTimeMs ?? null) : null),
    bufferedAmount: hb?.bufferedAmount ?? null,
    serverBufferedAmount: input.serverBufferedAmount ?? null,
    maxEpisodeId,
  };
}

/**
 * Evaluate the matrix. First matching row wins, in the design's order; the
 * default outcome is `healthy-idle` (a quietly listening user is not an
 * incident — row 5′).
 */
export function evaluateMatrix(input: MatrixInput): MatrixResult {
  const { snapshot: s, prev, now } = input;
  const hb = s.lastHeartbeat;
  const baseline = makeBaseline(input);
  let facts: MatrixFacts = FACTS_UNAVAILABLE;
  const out = (verdict: MatrixVerdict, ...reasons: string[]): MatrixResult => ({
    verdict,
    reasons,
    baseline,
    facts,
  });

  // ── Row 0: reconnect window / structural insufficiency ──
  if (input.sessionState === 'RECONNECTING' || input.sessionState === 'CONNECTING') {
    return out('reconnect-window', `session=${input.sessionState}`);
  }
  if (!input.clientConnected) {
    return out('healthy-idle', 'no-client-attached');
  }
  if (!hb || !prev || prev.epoch !== s.epoch) {
    // No heartbeat yet, no prior tick, or an epoch boundary since the last
    // tick: nothing below can be computed as a same-epoch delta — never
    // guess (a cross-epoch diff is negative garbage that still matches).
    return out(
      'insufficient-evidence',
      !hb ? 'no-client-heartbeat' : !prev ? 'first-tick-no-baseline' : 'epoch-boundary',
    );
  }
  const hbStale = now - hb.receivedAt > 8_000;

  const dDelivered = s.deliveredFrames - prev.deliveredFrames;
  const dCapCallbacks = s.clientTotals.capCallbacks - prev.capCallbacks;
  const dBytesSent = s.clientTotals.bytesSent - prev.bytesSent;
  const dSendSkipped = s.clientTotals.sendSkipped - prev.sendSkipped;
  const dChunksRecv = s.clientTotals.chunksRecv - prev.chunksRecv;
  const dChunksScheduled = s.clientTotals.chunksScheduled - prev.chunksScheduled;
  const dChunksEnded = s.clientTotals.chunksEnded - prev.chunksEnded;
  const dChunksCancelled = s.clientTotals.chunksCancelled - prev.chunksCancelled;
  const muted = hb.muted;
  const ingressStalled =
    s.lastDeliveredAt === null || now - s.lastDeliveredAt > MATRIX_INGRESS_STALL_MS;

  // Facts are computed once the same-epoch deltas exist, and attach to EVERY
  // result from here down — they are observations, not verdicts.
  const factsSpeechInWindow =
    s.speech.active ||
    (s.speech.lastAboveFloorAt !== null && now - s.speech.lastAboveFloorAt < 30_000);
  facts = {
    factsAvailable: true,
    speechInWindow: factsSpeechInWindow,
    speechObservedAt: factsSpeechInWindow
      ? (s.speech.onsetAt ?? s.speech.lastOnsetAt ?? s.speech.lastAboveFloorAt)
      : null,
    ingressAdvanced: dDelivered > 0,
    modelSilentFor15s:
      input.lastModelEventAt != null && now - input.lastModelEventAt > 15_000,
  };

  // ── Row 1: client capture dead/suspended ──
  // A CURRENT-epoch, UNEXPIRED gap (or the open gap itself) whose interval
  // overlaps the ingress stall, while unmuted + connected. An old latched
  // gap must never pair with unrelated later silence (round-3 #3).
  if (!muted && ingressStalled) {
    const stallStartedAt = s.lastDeliveredAt ?? now - MATRIX_INGRESS_STALL_MS;
    let evidence: string | null = null;
    if (hb.openGap && !hbStale) {
      const gapStartAbs = hb.receivedAt - hb.openGap.ageMs;
      if (gapStartAbs <= stallStartedAt + 2_000) evidence = `open-gap age=${hb.openGap.ageMs}ms`;
    }
    if (!evidence && s.epochStartApproxMs !== null) {
      for (const e of hb.episodes) {
        if (e.kind !== 'gap' || e.startMs === undefined || e.durationMs === undefined) continue;
        const endAbs = s.epochStartApproxMs + e.startMs + e.durationMs;
        if (now - endAbs > EPISODE_UNEXPIRED_MS) continue; // expired — not current evidence
        // Overlap (±2 s receipt-time tolerance): the gap must cover part of
        // the ingress stall, not merely precede it.
        if (endAbs >= stallStartedAt - 2_000) {
          evidence = `gap-episode id=${e.id} dur=${e.durationMs}ms`;
          break;
        }
      }
    }
    if (evidence) {
      // Discriminate the S1/S2 shapes from the episode fields that travel.
      const shape =
        hb.ctxState === 's'
          ? 'ctx-suspended'
          : hb.ctxSuspendCount > 0
            ? 'post-suspension'
            : hb.captureState === 'r' || hb.captureState === 'd'
              ? 'device-recovery'
              : 'main-thread-gap';
      return out('client-capture-dead', evidence, `shape=${shape}`, `ctx=${hb.ctxState ?? '?'}`);
    }
  }

  // ── Rows 2–4: native server dispositions — unreachable at session-only ──
  if (s.coverage !== ('full' as string)) {
    // The patterns these rows need (received/buffered/eligible/sdkSend
    // counters) do not exist yet; if the observable half of the pattern is
    // present, say WHY the verdict cannot be issued.
    if ((dCapCallbacks > 0 || dBytesSent > 0) && dDelivered === 0 && !muted && !hbStale) {
      return out(
        'insufficient-evidence',
        'client-sending-but-session-delivery-stalled',
        'server-ingress-chain-unobserved(coverage:session-only)',
      );
    }
  }

  // ── Row 5: model silent (evaluated BEFORE rows 6/7 — the D7.2 table
  // order; speech with no model response outranks backpressure findings).
  // Speech evidence is the CANONICAL server-ingress tracker only — a stale
  // client speech episode idempotently re-sent in the heartbeat window is
  // NOT current-window evidence.
  const speech = s.speech;
  const speechInWindow =
    speech.active || (speech.lastAboveFloorAt !== null && now - speech.lastAboveFloorAt < 30_000);
  if (speechInWindow && dDelivered > 0) {
    const modelSilent =
      input.lastModelEventAt == null || now - input.lastModelEventAt > 15_000;
    if (modelSilent) {
      if (s.coverage !== ('full' as string)) {
        // Speech reached the session and nothing came back — but the
        // upstream-send hop is unobserved, so blaming the model would be a
        // guess (the design's honesty rule).
        return out(
          'insufficient-evidence',
          'speech-without-model-event',
          'upstream-hop-unobserved(coverage:session-only)',
        );
      }
      return out('model-silent', 'speech-in-window', 'no-model-event');
    }
  }

  // ── Row 6: client send backpressure (FE-1 class, client side) ──
  const bufferedGrowing =
    hb.bufferedAmount !== null &&
    prev.bufferedAmount !== null &&
    hb.bufferedAmount > prev.bufferedAmount &&
    hb.bufferedAmount > 0;
  if (bufferedGrowing || dSendSkipped > 0) {
    return out(
      'client-send-backpressure',
      `bufferedAmount=${hb.bufferedAmount ?? 'n/a'}`,
      `sendSkipped+=${dSendSkipped}`,
    );
  }

  // ── Row 7a: server→client egress backpressure ──
  if (input.serverBufferedAmount != null && prev.serverBufferedAmount != null) {
    if (
      input.serverBufferedAmount > prev.serverBufferedAmount &&
      input.serverBufferedAmount > 0 &&
      dChunksRecv === 0 &&
      s.egressFrames > 0
    ) {
      return out('egress-backpressure', `serverBuffered=${input.serverBufferedAmount}`);
    }
  }

  // ── Row 7b: client playback failure ──
  // Scheduling proves delivery; NATURAL completion + ctx-clock advancement
  // prove playback. Cancellations are excluded BOTH ways: chunksEnded never
  // counts them, and a window where (nearly) everything scheduled was
  // cancelled (barge-in flush) proves nothing about the output path.
  const dEffectiveScheduled = dChunksScheduled - dChunksCancelled;
  if (dEffectiveScheduled >= PLAYBACK_MIN_CHUNKS && dChunksEnded === 0) {
    const ctxAdvanced =
      hb.ctxTimeMs !== null && prev.ctxTimeMs !== null && hb.ctxTimeMs > prev.ctxTimeMs;
    if (!ctxAdvanced) {
      return out(
        'client-playback-failure',
        `scheduled+=${dChunksScheduled}`,
        `cancelled+=${dChunksCancelled}`,
        'no-natural-completions',
        `ctxTime=${hb.ctxTimeMs ?? 'n/a'}`,
      );
    }
  }

  // ── Totality before the idle default (round-3 #3): a verdict of
  // healthy-idle must rest on EVIDENCE of health, not its absence. ──
  if (hb.episodeOverflow > 0) {
    // Episode evidence was lost client-side; the window cannot prove the
    // quiet period was quiet.
    return out('insufficient-evidence', `episodeOverflow=${hb.episodeOverflow}`);
  }
  if (hbStale) {
    return out('insufficient-evidence', 'heartbeat-stale');
  }
  if (!muted && dCapCallbacks === 0 && dDelivered === 0) {
    // Nothing progressed anywhere this window while unmuted: that is not a
    // quietly listening user, it is missing telemetry (row 5′ requires the
    // layers to be ADVANCING).
    return out('insufficient-evidence', 'no-progress-in-window');
  }

  // ── Row 5′: a quietly listening user is not an incident. ──
  return out('healthy-idle');
}
