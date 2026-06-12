// voice-core — the shared brain for all Sutando voice surfaces.
//
// Consumers:
//   1. src/voice-agent.ts (desktop surface, in-repo)
//   2. sonichi/sutando-meeting discord-voice plugin (out-of-repo, installs
//      this repo as a commit-pinned git dependency and imports
//      'sutando/voice-core' via the package.json exports map)
//
// SHARED SURFACE (fixed at these 6 items — Mini amendment #5; growing it
// requires bumping VOICE_CORE_API_VERSION and updating the plugin's contract
// test):
//   1. mode state machine + resolver            (mode-state.ts)
//   2. switch_mode / save_meeting_note factories (mode-tools.ts)
//   3. mode/meeting prompt strings               (prompts.ts)
//   4. session-resilience watchdog predicate     (session-resilience.ts)
//   5. recording API + surface-table registration (recording.ts)
//   6. this version constant
//
// Plugins MUST assert the version at startup and fail loudly on skew:
//   if (VOICE_CORE_API_VERSION !== EXPECTED) throw new Error(
//     'voice-core API skew — re-pin sutando and re-run the contract test');

export const VOICE_CORE_API_VERSION = 1;

export {
	type BaseMode,
	type ModeState,
	type ModeStateHandle,
	resolveCurrentMode,
	isPresenterActiveDefault,
	createModeState,
} from './mode-state.js';

export { makeSwitchModeTool, makeSaveMeetingNoteTool } from './mode-tools.js';

export {
	ACTIVE_MARKER,
	MEETING_MARKER,
	PRESENTER_MARKER,
	SWITCH_MODE_DESCRIPTION,
	MEETING_ENTER_INSTRUCTION,
	ACTIVE_ENTER_INSTRUCTION,
	RULE_MEETING_ACTIVE,
	RULE_MEETING_AVAILABLE,
	RULE_MEETING_ANSWER_DIRECT,
	RULE_WHEN_IN_DOUBT,
} from './prompts.js';

export {
	type WatchdogInputs,
	DEFAULT_WATCHDOG_THRESHOLD_MS,
	shouldWatchdogReconnect,
} from './session-resilience.js';

export {
	recordConversation,
	recordEvent,
	recordSession,
	recordToolCall,
	registerSurfaceTable,
	sourceFromRole,
	kindFromRole,
} from './recording.js';
