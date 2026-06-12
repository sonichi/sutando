// voice-core/session-recovery.ts — surface-agnostic reconnect-recovery
// policy shared by voice surfaces (Susan 2026-06-12 #susan thread: the
// reconnect policy lives upstream ONCE; surfaces keep only their own
// provenance state).
//
// Two decisions, both pure:
//
// 1. reconnectGreeting — "given current mode state, should this reconnect
//    greet or stay silent?" (Mini's interface). Extracted from the
//    voice-agent reconnect-context builder's inline ternary; the tuned
//    prompt STRINGS stay in the surface (prompts are surface UX), only the
//    selection policy moved here.
//
// 2. shouldRestoreActiveOnReconnect — after a hang→watchdog reconnect,
//    restore ACTIVE? Extracted verbatim from the discord-voice plugin's
//    session-recovery helper (#1427/#1600 reconnect-mute fix). The
//    `enteredDeliberately` parameter is SURFACE-SUPPLIED provenance (e.g.
//    discord's sticky meetingEntered bit): a deliberate "stand by" must
//    survive the reconnect (stay a silent note-taker), while an auto/stale
//    meeting mode must NOT strand the bot muted mid-conversation. The
//    module deliberately does NOT infer deliberateness from current mode
//    state — after a reconnect, mode polling alone cannot distinguish a
//    normal active chat from a meeting session that must continue silently
//    (Mini, 2026-06-12).

export interface ReconnectGreetingInputs {
	/** meeting mode currently resolved (silent note-taker). */
	isMeeting: boolean;
	/** presenter mode active — never interrupt a live talk. */
	isPresenter: boolean;
	/** reconnect gap below the network-blip threshold (caller decides, e.g. <60s). */
	quickReconnect: boolean;
}

export type ReconnectGreeting = 'meeting-notes' | 'silent' | 'greet';

/**
 * Greeting policy on user reconnect. Priority: meeting (stay the silent
 * note-taker, replay context) > quick-blip/presenter (say nothing — the
 * user wants to resume without UX interruption) > normal away-and-back
 * (one brief "Welcome back").
 */
export function reconnectGreeting(i: ReconnectGreetingInputs): ReconnectGreeting {
	if (i.isMeeting) return 'meeting-notes';
	if (i.quickReconnect || i.isPresenter) return 'silent';
	return 'greet';
}

/**
 * Watchdog-reconnect-mute fix (#1427, hardened in #1600): after a Gemini
 * hang→reconnect, restore ACTIVE only when meeting mode was NOT deliberately
 * entered — auto/stale mute recovers, a deliberate "stand by" persists.
 */
export function shouldRestoreActiveOnReconnect(meetingMode: boolean, enteredDeliberately: boolean): boolean {
	return meetingMode && !enteredDeliberately;
}
