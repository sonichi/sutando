/** Pure predicate + state machine, no deps — importable so tests exercise THIS
 *  code rather than a copy that can pass while the real sanitizer drifts. */

/** Matches a turn that opens with a fabricated control/metadata directive. */
export const FABRICATED_OUTPUT_RE = /^\s*(\[System:|System:|\[Silence\.?\]|<ctrl\d+>)/i;

/** Call on the RUNNING buffer: deltas arrive split ("[Sys" | "tem: ..."), so the
 *  ^-anchor matches on neither fragment alone. */
export function isFabricatedOutput(buffered: string): boolean {
	return FABRICATED_OUTPUT_RE.test((buffered ?? '').trim());
}

/** Anchored alternatives all start '[', 'S'/'s' or '<', so an ordinary turn
 *  diverges within a couple of chars and flushes; only a real prefix is held. */
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

/** What transport.onOutputTranscription runs. Extracted from the wiring IIFE so
 *  the suite drives the real machine, not a mirrored copy of it. */
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

/** The transport surface the wiring touches. Both fields are optional because
 * GeminiLiveTransport and OpenAIRealtimeTransport expose different subsets. */
export interface SanitizableTransport {
	onAudioOutput?: (b64: string) => void;
	onOutputTranscription?: (text: string) => void;
}

export interface SanitizerWiring {
	transport: SanitizableTransport | null | undefined;
	subscribe: (event: string, handler: () => void) => void;
	/** Run `reset` immediately BEFORE the host finalizes its transcript. The
	 * host publishes turn.end AFTER flushing, so a subscriber alone is late. */
	beforeTranscriptFlush?: (reset: () => void) => void;
	onBlocked?: (buffered: string) => void;
	log?: (message: string) => void;
}

/** Install the sanitizer between the transport and its consumers.
 *
 * Audio is gated by CLOSING OVER a local flag and wrapping the transport's own
 * callback — never by probing a `_suppressAudio` field, which only one transport
 * defines and which is absent at wiring time on the other.
 */
export function wireSanitizerToTransport(deps: SanitizerWiring): boolean {
	const transport = deps.transport;
	if (!transport) return false;
	let suppressAudio = false;
	const origOnAudioOutput = transport.onAudioOutput?.bind(transport);
	transport.onAudioOutput = (b64: string) => { if (suppressAudio) return; origOnAudioOutput?.(b64); };
	const origOnOutputTranscription = transport.onOutputTranscription?.bind(transport);
	const sanitizer = createOutputSanitizer({
		forward: (t) => origOnOutputTranscription?.(t),
		setSuppressAudio: (on) => { suppressAudio = on; },
		onBlocked: deps.onBlocked,
	});
	transport.onOutputTranscription = (text: string) => sanitizer.handleChunk(text);
	const resetTurn = () => sanitizer.resetTurn();
	// resetTurn forwards still-held clean text, so it must run before the host
	// flushes. Idempotent, so the event subscriptions remain a safe backstop.
	deps.beforeTranscriptFlush?.(resetTurn);
	deps.subscribe('turn.end', resetTurn);
	deps.subscribe('turn.interrupted', resetTurn);
	deps.log?.('[OutputSanitizer] wired into transport.onOutputTranscription (per-turn buffered)');
	return true;
}
