// voice-core/session-resilience.ts — surface-agnostic session-health
// primitives shared by voice surfaces.
//
// Scope note (refactor round ①/②): this module holds the PURE watchdog
// predicate that decides when a live session has gone decision-dead and must
// be force-reconnected (the #1600 M-series fix: old predicate watched audio
// frames and scored 0/34 on real hangs; the hardened predicate watches
// turn-level decision activity). The reconnect CONTEXT RE-INJECTION remains
// in each surface's server for now — it is entangled with per-surface session
// state and moves here only after the round-③ live test (see the requirement
// doc §6).

export interface WatchdogInputs {
	/** ms timestamp of the last turn-level decision activity (speak_decision row / turn close). */
	lastTurnActivityTs: number;
	/** current ms timestamp (pass Date.now() — parameter keeps the fn pure/testable). */
	now: number;
	/** silence threshold in ms before declaring the decision pipeline dead. */
	thresholdMs?: number;
	/** true while a human is actively in the channel — no humans means silence is expected. */
	humansPresent: boolean;
}

export const DEFAULT_WATCHDOG_THRESHOLD_MS = 60_000;

/**
 * True when the decision pipeline has been silent past the threshold while
 * humans are present — the signature of a wedged Gemini session that still
 * holds the transport open. Caller should respond with a transport-close
 * (code 4002) so the standard reconnect path revives the pipeline.
 */
export function shouldWatchdogReconnect(i: WatchdogInputs): boolean {
	if (!i.humansPresent) return false;
	const threshold = i.thresholdMs ?? DEFAULT_WATCHDOG_THRESHOLD_MS;
	return i.now - i.lastTurnActivityTs > threshold;
}
