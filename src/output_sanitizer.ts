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

/**
 * Prefixes a held buffer might still be growing into. The anchored alternatives all
 * begin with '[', 'S'/'s', or '<', so an ordinary turn diverges within a couple of
 * characters and is flushed immediately; only a real prefix is held.
 */
export const FAB_PREFIXES = ['[system:', 'system:', '[silence', '<ctrl'];

/** True while the buffer could still become a fabricated directive. */
export function couldStillBeFabrication(raw: string): boolean {
	const s = (raw ?? '').replace(/^\s+/, '').toLowerCase();
	if (s.length === 0) return true;   // only whitespace so far — undecided
	if (s.length > 24) return false;   // safety cap: far past any real fabricated prefix
	return FAB_PREFIXES.some((p) => p.startsWith(s) || s.startsWith(p));
}

/** Side-effecting edges the stream needs, injected so a test can observe them. */
export interface OutputSanitizerHooks {
	/** Deliver text onward to the real transcript consumer. */
	forward: (text: string) => void;
	/** Best-effort audio suppression toggle (no-op where the transport lacks it). */
	setSuppressAudio?: (on: boolean) => void;
	/** Called once per turn when a fabricated directive is blocked. */
	onBlocked?: (buffered: string) => void;
}

export interface OutputSanitizerStream {
	/** Feed one streamed transcript delta. */
	handleChunk: (text: string) => void;
	/** Turn boundary: flush still-held CLEAN text and clear per-turn state. */
	resetTurn: () => void;
}

/**
 * The per-turn hold/suppress/flush state machine.
 *
 * THIS is what `transport.onOutputTranscription` runs, and therefore what the tests
 * must drive. It used to live inline in `main()`'s wiring IIFE in voice-agent.ts,
 * which made it unimportable — so the suite reimplemented the same hold/flush/reset
 * logic locally and could stay green while production drifted. qingyun rejected that
 * twice on #1414: *"the copied state machine in the test remains correct"* even if the
 * real wrapper flushed `heldText` too early, failed to reset, or stopped setting
 * `_suppressAudio`. A mirrored state machine is not a test of the real one — it is the
 * same belief written twice. Extraction is what makes the guarantee structural rather
 * than a promise to keep two copies in sync.
 *
 * Behaviour is byte-for-byte the in-IIFE original: same ordering, same guards, same
 * best-effort `_suppressAudio` handling, same try/catch around the boundary flush.
 */
export function createOutputSanitizer(hooks: OutputSanitizerHooks): OutputSanitizerStream {
	let outputBuffer = '';   // running per-turn buffer, anchored at the turn's start
	let heldText = '';       // clean output held back until proven NOT a fabrication prefix
	let turnFabricated = false;
	let turnCleared = false; // once true, this turn is confirmed clean → stream directly

	const handleChunk = (text: string): void => {
		const chunk = text ?? ''; // guard: null/undefined delta must not throw the pipeline
		if (turnFabricated) return;                       // already suppressed
		if (turnCleared) { hooks.forward(chunk); return; } // confirmed clean → stream
		outputBuffer += chunk;
		heldText += chunk;                                 // hold; do not forward yet
		if (isFabricatedOutput(outputBuffer)) {
			hooks.onBlocked?.(outputBuffer);
			turnFabricated = true;
			// Best-effort: suppress remaining audio chunks in this turn.
			hooks.setSuppressAudio?.(true);
			return; // nothing held is ever forwarded — no split-chunk leak
		}
		if (!couldStillBeFabrication(outputBuffer)) {      // diverged → clean
			turnCleared = true;
			const flush = heldText; heldText = '';
			hooks.forward(flush);
		}
	};

	// Flush any still-held CLEAN text so a short turn that ended mid-hold (e.g. the whole
	// turn was just "Sure") isn't dropped.
	const resetTurn = (): void => {
		if (heldText && !turnFabricated) { try { hooks.forward(heldText); } catch { /* best-effort */ } }
		outputBuffer = '';
		heldText = '';
		turnFabricated = false;
		turnCleared = false;
		hooks.setSuppressAudio?.(false);
	};

	return { handleChunk, resetTurn };
}
