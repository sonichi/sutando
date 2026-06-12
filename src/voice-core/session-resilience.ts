// voice-core/session-resilience.ts — surface-agnostic session-health
// primitives shared by voice surfaces.
//
// Provenance: isSessionHung is the EXACT hung-session condition extracted
// from the discord-voice server's inline watchdog loop (the #1600 M-series
// hardened predicate) — not a re-derivation. The interval wiring, the
// recordEvent observability row, and the forced transport-close (4002)
// recovery stay in the surface's server; only the pure decision moved here.
//
// Original commentary (moved with the condition):
// A healthy Gemini fires turn.end within seconds of the user finishing an
// utterance. If >=2 utterances have piled up since the last turn activity
// and the user last stopped speaking more than stallMs ago, the session has
// silently stalled. The >=2 guard keeps a single Gemini-ignored
// micro-utterance from tripping it. The second trigger (#1600) decouples
// detection from user silence: the silence-based trigger is suppressed while
// users keep talking (every utterance resets idleMs — the 06-10 10:58 hang
// stayed undetected for 4+ min because the users never paused >20s).
// Utterances piling up with NO turn activity for hungTurnMs is a hang
// regardless of continued speech: healthy sessions complete turns every
// 10-25s even in continuous group chat, and turns fire even when the output
// gate mutes audio, so a 60s turn-drought with a backlog is unambiguous.

export interface SessionHangInputs {
	/** ms timestamp the user last stopped speaking (lastSpeakStopTs). */
	lastSpeakStopTs: number;
	/** ms timestamp of the last turn-level activity (lastTurnActivityTs). */
	lastTurnActivityTs: number;
	/** utterances accumulated since the last turn activity (utterancesSinceTurn). */
	utterancesSinceTurn: number;
	/** current ms timestamp (pass Date.now() — parameter keeps the fn pure/testable). */
	now: number;
	/** silence trigger threshold; surfaces may env-override (SUTANDO_WATCHDOG_STALL_MS). */
	stallMs?: number;
	/** turn-drought trigger threshold; surfaces may env-override (SUTANDO_WATCHDOG_HUNG_TURN_MS). */
	hungTurnMs?: number;
}

export const DEFAULT_WATCHDOG_STALL_MS = 20_000;
export const DEFAULT_WATCHDOG_HUNG_TURN_MS = 60_000;

/**
 * True when the session is hung — the verbatim watchdog condition:
 * utterances piled up past the last turn AND (user-silence stall OR
 * turn-drought). Caller responds with a forced transport close so the
 * standard reconnect path revives the decision pipeline.
 */
export function isSessionHung(i: SessionHangInputs): boolean {
	const stop = i.lastSpeakStopTs || 0;
	const turn = i.lastTurnActivityTs || 0;
	const pile = i.utterancesSinceTurn || 0;
	const idleMs = i.now - stop;
	const sinceTurnMs = i.now - turn;
	const stallMs = i.stallMs ?? DEFAULT_WATCHDOG_STALL_MS;
	const hungTurnMs = i.hungTurnMs ?? DEFAULT_WATCHDOG_HUNG_TURN_MS;
	return stop > turn && pile >= 2 && (idleMs > stallMs || sinceTurnMs > hungTurnMs);
}
