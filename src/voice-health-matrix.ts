// voice-health-matrix — P7 D7.2: the failure-localization matrix. A pure
// function over the engine ledger snapshot with HONEST outcomes: a partial
// ledger (coverage 'session-only', missing heartbeats, a reconnect window)
// can never produce a confident verdict about the layers the partiality
// obscures. Rows 2–4 (native server dispositions) therefore stay unreachable
// until the bodhi tranche flips coverage to 'full'; their patterns surface as
// `insufficient-evidence` with the blind hop named. Evaluated per 30 s health
// tick; the caller logs the verdict and notes it into the ledger for
// persistence.

import type { AudioHealthSnapshot, LedgerCoverage } from './voice-audio-health.js';
import { observesUpstreamSend } from './voice-audio-health.js';

export type MatrixVerdict =
  | 'reconnect-window'
  | 'insufficient-evidence'
  | 'client-capture-dead' // row 1
  | 'server-accounting-inconsistency' // row 2 [needs native counters]
  | 'server-discarding' // row 3 [needs native counters]
  | 'upstream-send-dead' // row 4 [needs native counters]
  | 'upstream-send-failing' // row 4b — attempts happened, none succeeded
  | 'post-sdk-silent' // row 5 (renamed from the aspirational 'model-silent')
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
  /** Upstream (agent→SDK) counters — null while unobserved. Deltas NEVER cross
   *  a transport generation; the structural guard downgrades that window. */
  transportGeneration: number | null;
  audioAttempted: number | null;
  audioQueued: number | null;
  /** Row 4b names the guard outcome from WINDOW deltas, not
   *  generation-lifetime totals — these are the diff sources. */
  audioSkippedNoSession: number | null;
  audioThrew: number | null;
  echoSuppressed: number | null;
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
  /** Coverage the EVALUATOR runs at. Defaults to the snapshot's own level; the
   *  Phase-1 shadow passes 'session+egress' to exercise rows 4/4b/5 without
   *  touching the live verdict stream. */
  effectiveCoverage?: LedgerCoverage;
  now: number;
}

/** Verdict-independent observations (design §1.5): the watchdog integration
 *  reads only facts, never verdict strings, so verdict-independence holds
 *  under every coverage mode. Every field is false/null whenever the window
 *  cannot prove it (unobserved upstream, no valid baseline, early
 *  structural returns).
 *
 *  Two fact families live here. The upstream-send family is coverage-blind
 *  by construction; the ACTIVE-silence family (design
 *  design-voice-active-silence-recovery.md §Trigger (a)) additionally needs
 *  a same-epoch baseline, which `factsAvailable` reports.
 *
 *  `factsAvailable` says the data existed, never that acting is safe:
 *  consumers of the ACTIVE-silence family own their own structural gating. */
export type MatrixFacts = {
  attemptedAudioAdvanced: boolean;
  queuedAudioAdvanced: boolean;
  losslessWindowWithSpeechQueued: boolean;
  transportGenerationChanged: boolean;
  echoSuppressedAdvanced: boolean;
  /** Same-epoch baseline + heartbeat existed this tick; false ⇒ every
   *  ACTIVE-silence fact below is false/null. */
  factsAvailable: boolean;
  speechInWindow: boolean;
  /** Monotonic timestamp of the retained speech observation's first
   *  above-floor sample; non-null exactly when speechInWindow. The one
   *  non-boolean member — ledger consumers widen accordingly. */
  speechObservedAt: number | null;
  ingressAdvanced: boolean;
  modelSilentFor15s: boolean;
};

export interface MatrixResult {
  verdict: MatrixVerdict;
  reasons: string[];
  facts: MatrixFacts;
  baseline: MatrixBaseline;
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
    transportGeneration: s.transportGeneration,
    // NOT `?? 0`: a zero baseline is indistinguishable from an unobserved one,
    // and a delta taken against it is a lifetime total wearing a window's clothes.
    audioAttempted: s.upstream?.audio.attempted ?? null,
    audioQueued: s.upstream?.audio.queued ?? null,
    audioSkippedNoSession: s.upstream?.audio.skippedNoSession ?? null,
    audioThrew: s.upstream?.audio.threw ?? null,
    echoSuppressed: s.echoSuppressed,
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
  const coverage = input.effectiveCoverage ?? s.coverage;

  // ── Facts: computed FIRST, from DATA VALIDITY alone — never from the
  // verdict path taken and never from coverage (§1.5: the watchdog reads
  // facts and must behave identically under every coverage mode and
  // verdict; coverage gates which VERDICTS may be claimed, below). ──
  const winPrev = prev !== null && prev.epoch === s.epoch ? prev : null;
  const au = s.upstream?.audio ?? null;
  const genChanged =
    winPrev !== null &&
    winPrev.transportGeneration !== null &&
    s.transportGeneration !== null &&
    s.transportGeneration !== winPrev.transportGeneration;
  // Counter deltas are meaningful only with an adjacent SAME-epoch,
  // SAME-generation, observed-on-BOTH-ends baseline — a null generation on
  // either side (codex round-3 #2) makes them generation-lifetime garbage.
  const upstreamWindowObserved =
    winPrev !== null &&
    au !== null &&
    winPrev.audioAttempted !== null &&
    winPrev.transportGeneration !== null &&
    s.transportGeneration !== null &&
    !genChanged;
  const dDelivered = winPrev !== null ? s.deliveredFrames - winPrev.deliveredFrames : 0;
  const dAttemptedAudio =
    upstreamWindowObserved && au !== null && winPrev !== null
      ? Math.max(0, au.attempted - (winPrev.audioAttempted as number))
      : 0;
  const dQueuedAudio =
    upstreamWindowObserved && au !== null && winPrev !== null
      ? Math.max(0, au.queued - (winPrev.audioQueued as number))
      : 0;
  const dSkippedNoSession =
    upstreamWindowObserved && au !== null && winPrev !== null
      ? Math.max(0, au.skippedNoSession - (winPrev.audioSkippedNoSession as number))
      : 0;
  const dThrewAudio =
    upstreamWindowObserved && au !== null && winPrev !== null
      ? Math.max(0, au.threw - (winPrev.audioThrew as number))
      : 0;
  const dEchoSuppressed =
    winPrev !== null && s.echoSuppressed !== null && winPrev.echoSuppressed !== null
      ? Math.max(0, s.echoSuppressed - winPrev.echoSuppressed)
      : 0;
  const speech = s.speech;
  const speechInWindow =
    speech.active || (speech.lastAboveFloorAt !== null && now - speech.lastAboveFloorAt < 30_000);
  const queuedAfterSpeech =
    au !== null &&
    au.lastQueuedAt !== null &&
    speech.lastAboveFloorAt !== null &&
    au.lastQueuedAt >= speech.lastAboveFloorAt;
  const lossless = dAttemptedAudio >= dDelivered && dQueuedAudio === dAttemptedAudio;
  // The ACTIVE-silence family needs the same-epoch baseline the structural
  // early returns below bail on; without it every member fails closed.
  const factsAvailable = hb != null && winPrev !== null;
  const facts: MatrixFacts = {
    attemptedAudioAdvanced: dAttemptedAudio > 0,
    queuedAudioAdvanced: dQueuedAudio > 0,
    losslessWindowWithSpeechQueued:
      upstreamWindowObserved &&
      speechInWindow &&
      dDelivered > 0 &&
      lossless &&
      queuedAfterSpeech &&
      dEchoSuppressed === 0,
    transportGenerationChanged: genChanged,
    echoSuppressedAdvanced: dEchoSuppressed > 0,
    factsAvailable,
    speechInWindow: factsAvailable && speechInWindow,
    speechObservedAt:
      factsAvailable && speechInWindow
        ? (speech.onsetAt ?? speech.lastOnsetAt ?? speech.lastAboveFloorAt)
        : null,
    ingressAdvanced: factsAvailable && dDelivered > 0,
    modelSilentFor15s:
      factsAvailable && input.lastModelEventAt != null && now - input.lastModelEventAt > 15_000,
  };
  // What the EVALUATOR may claim from the window — the only coverage-gated half.
  const upstreamWindowValid = observesUpstreamSend(coverage) && upstreamWindowObserved;
  const out = (verdict: MatrixVerdict, ...reasons: string[]): MatrixResult => ({
    verdict,
    reasons,
    facts,
    baseline,
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
  // ── Structural guard, BEFORE every delta-based row: upstream counters reset
  // per transport generation, so every delta and last…At is garbage across a
  // reconnect (design §1.5). The FACT is recorded above, coverage-independently;
  // only this verdict-side return is gated. NOTE this guard is gated on
  // observesUpstreamSend, so BELOW session+egress it does not fire — row 5 is
  // protected there by its own honesty branch, not by this. Relaxing that branch
  // without widening this guard loses row 5's protection. ──
  if (observesUpstreamSend(coverage) && genChanged) {
    return out('insufficient-evidence', 'transport-generation-changed');
  }
  const hbStale = now - hb.receivedAt > 8_000;

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

  // ── Rows 2–3: server-side dispositions — unreachable below 'full' ──
  if (coverage !== 'full') {
    // The patterns these rows need (received/buffered/eligible counters) do
    // not exist; if the observable half of the pattern is present, say WHY
    // the verdict cannot be issued.
    if ((dCapCallbacks > 0 || dBytesSent > 0) && dDelivered === 0 && !muted && !hbStale) {
      return out(
        'insufficient-evidence',
        'client-sending-but-session-delivery-stalled',
        'server-ingress-chain-unobserved(coverage:session-only)',
      );
    }
  }

  // ── Rows 4/4b: the agent→SDK hop (design §1.5) — need 'session+egress'
  // AND a valid window (an unobserved baseline proves nothing). ──
  if (
    upstreamWindowValid &&
    input.sessionState === 'ACTIVE' &&
    !muted &&
    dDelivered > 0 &&
    dEchoSuppressed === 0
  ) {
    if (dAttemptedAudio === 0) {
      // Eligible ingress arrived and the agent never even called sendAudio —
      // a DEAD call path, not a failing one. In-process defect.
      return out('upstream-send-dead', `delivered+=${dDelivered}`, 'attempted+=0');
    }
    if (dQueuedAudio === 0) {
      // Named by THIS window's guard outcome — lifetime totals would let
      // earlier skips masquerade as the current failure mode.
      return out(
        'upstream-send-failing',
        `attempted+=${dAttemptedAudio}`,
        'queued+=0',
        `skippedNoSession+=${dSkippedNoSession}`,
        `threw+=${dThrewAudio}`,
      );
    }
  }

  // ── Row 5: model silent (evaluated BEFORE rows 6/7 — the D7.2 table
  // order; speech with no model response outranks backpressure findings).
  // Speech evidence is the CANONICAL server-ingress tracker only — a stale
  // client speech episode idempotently re-sent in the heartbeat window is
  // NOT current-window evidence.
  if (speechInWindow && dDelivered > 0) {
    const modelSilent =
      input.lastModelEventAt == null || now - input.lastModelEventAt > 15_000;
    if (modelSilent) {
      if (!observesUpstreamSend(coverage) || au === null) {
        // Speech reached the session and nothing came back — but the
        // upstream-send hop is unobserved, so blaming anything past the
        // agent would be a guess (the design's honesty rule).
        return out(
          'insufficient-evidence',
          'speech-without-model-event',
          'upstream-hop-unobserved(coverage:session-only)',
        );
      }
      if (!upstreamWindowValid) {
        // The window is not observed on BOTH ends (null generation on
        // either side): diffing counters would fabricate `post-sdk-silent`
        // from generation-lifetime totals.
        return out(
          'insufficient-evidence',
          'speech-without-model-event',
          'upstream-window-unobserved',
        );
      }
      // Lossless-window proof (design §1.5): aggregate counters cannot say
      // WHICH frame queued, so require that NOTHING was dropped — then the
      // speech-bearing frames were necessarily queued.
      if (facts.losslessWindowWithSpeechQueued) {
        return out(
          'post-sdk-silent',
          'speech-in-window',
          'lossless-window',
          `queued+=${dQueuedAudio}`,
          'no-model-event',
        );
      }
      return out(
        'insufficient-evidence',
        'speech-without-model-event',
        dEchoSuppressed > 0
          ? 'echo-suppressed-window'
          : lossless
            ? 'speech-queue-order-unproven'
            : 'lossy-window',
      );
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
