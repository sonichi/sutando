/**
 * Output sanitizer (fix #1 of #1410) — pure predicate, no side effects, no deps.
 *
 * Extracted from `src/voice-agent.ts` so tests can exercise THIS implementation
 * rather than a copy of it. The previous test declared its own regex with a
 * comment saying it "MUST stay in sync" — a test that cannot fail when the real
 * sanitizer drifts (qingyun, #1414: "the added test mirrors sanitizer logic
 * instead of importing/exercising the production implementation, so it can pass
 * while the actual stream wiring regresses").
 *
 * Importing voice-agent.ts directly would work but drags its whole graph
 * (dotenv, transports) into a unit test. Same shape as the repo's existing pure
 * companions — discord_addressee.py, result_markers.py, reply_chain.py: the
 * async/wiring side stays in the caller, the truth table lives here and is
 * unit-tested directly.
 *
 * Behaviour is unchanged from the in-IIFE original: same pattern, same flags.
 */

/** Matches a turn that opens with a fabricated control/metadata directive. */
export const FABRICATED_OUTPUT_RE = /^\s*(\[System:|System:|\[Silence\.?\]|<ctrl\d+>)/i;

/**
 * True once the running per-turn buffer opens with a fabricated directive.
 *
 * The caller accumulates streamed deltas and calls this on the RUNNING buffer:
 * onOutputTranscription is fed incremental fragments, so a prefix split across
 * chunks ("[Sys" | "tem: …") matches the ^-anchor on neither fragment alone.
 * Anchoring on the accumulated buffer is what closes that gap.
 */
export function isFabricatedOutput(buffered: string): boolean {
	return FABRICATED_OUTPUT_RE.test((buffered ?? '').trim());
}
