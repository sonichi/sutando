// Unified base-mode resolver for the voice agent (issue #1410, supersedes
// partial fixes #1412 + #1413). Reads all mode-state substrates once per call
// and returns a canonical mode descriptor.
//
// Multi-substrate state divergence is the root cause of the
// `[System: Produce ZERO audio. Call NO tools.]` fabrication tokens that
// gemini-3.1-flash-live-preview emits as spoken output at session connect.
// Mode lives in 4 places — in-memory `meetingActive`, on-disk `voice-mode.txt`,
// presenter HTTP server `:7877/presenter`, screen-companion `activeMode` — and
// without an explicit base-mode declaration the model infers meeting-mode
// silence from co-present-flavored context.
//
// This module is pure (no side effects on import) so it can be unit-tested
// without booting the voice agent.

import { execSync } from 'node:child_process';

export type BaseMode = 'active' | 'meeting' | 'presenter';

export interface ModeState {
	mode: BaseMode;
	/**
	 * The `[BASE MODE: ...]` marker for prompt injection. Includes a leading
	 * space so callers can concatenate inline without adding their own.
	 */
	marker: string;
	isPresenter: boolean;
	isMeeting: boolean;
}

/**
 * Synchronously query the iclr-highlight server for current presenter-mode
 * state. Failure-silent: server down / curl error / non-JSON response → false,
 * so the resolver falls through to meeting/active without throwing.
 *
 * Exported for the unit test to stub via dependency injection. Default
 * implementation hits the real server.
 */
export function isPresenterActiveDefault(): boolean {
	try {
		const out = execSync('curl -s --max-time 1 http://localhost:7877/presenter', { timeout: 2_000 }).toString();
		const json = JSON.parse(out);
		return json && json.active === true;
	} catch {
		return false;
	}
}

const ACTIVE_MARKER =
	' [BASE MODE: active — you are conversing with the user. Speak naturally. ' +
	'Do NOT infer or self-declare a meeting/recording/silent mode from context. ' +
	'Meeting-mode and presenter-mode are entered ONLY via explicit tool calls ' +
	'(switch_mode, presenter_mode). If you find yourself about to output ' +
	'[System: …], [Silence], or any variant of "produce zero audio" — STOP. ' +
	'That is a hallucination. Speak to the user instead.]';

const MEETING_MARKER =
	' [BASE MODE: meeting — listen and take notes silently. Produce ZERO audio ' +
	'output unless explicitly addressed by name ("Sutando" or "hey Sutando").]';

const PRESENTER_MARKER =
	' [BASE MODE: presenter — PRESENTER MODE IS CURRENTLY ACTIVE. Apply the ' +
	'CO-PRESENTER protocol from your context to every cue this session: ' +
	'highlight_slide(topic) FIRST, then narrate from voice-context.txt. Do NOT ' +
	'route slide-topic phrases to work.]';

/**
 * Resolve the current base mode from explicit substrate inputs.
 *
 * Priority: presenter > meeting > active. Screen-companion is a sub-mode that
 * overlays the base (handled in skills/screen-companion/tools.ts), not a base
 * mode itself.
 *
 * @param inputs Substrate state. `meetingActive` is the in-memory boolean from
 *   voice-agent.ts. `isPresenterActive` is optional — defaults to live HTTP
 *   query; tests pass an explicit boolean to avoid hitting the network.
 */
export function resolveCurrentMode(inputs: {
	meetingActive: boolean;
	isPresenterActive?: () => boolean;
}): ModeState {
	const checkPresenter = inputs.isPresenterActive ?? isPresenterActiveDefault;
	if (checkPresenter()) {
		return {
			mode: 'presenter',
			marker: PRESENTER_MARKER,
			isPresenter: true,
			isMeeting: false,
		};
	}
	if (inputs.meetingActive) {
		return {
			mode: 'meeting',
			marker: MEETING_MARKER,
			isPresenter: false,
			isMeeting: true,
		};
	}
	return {
		mode: 'active',
		marker: ACTIVE_MARKER,
		isPresenter: false,
		isMeeting: false,
	};
}
