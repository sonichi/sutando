// voice-mode-resolver — MOVED to src/voice-core/mode-state.ts as part of the
// two-repo voice refactor (#1427). This shim preserves the old import path for
// existing consumers (unit tests, any out-of-tree references); new code should
// import from './voice-core/index.js'. Behavior is identical — the resolver
// was moved verbatim, with the marker strings now sourced from
// voice-core/prompts.ts (the single prompt home).

export {
	type BaseMode,
	type ModeState,
	isPresenterActiveDefault,
	resolveCurrentMode,
} from './voice-core/mode-state.js';
