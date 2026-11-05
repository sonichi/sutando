// The phone server's upstream-recovery wiring, importable without booting the server.
import type { EventPayloadMap, IEventBus, UpstreamRecoveryOptions } from 'bodhi-realtime-agent';
import { fatalCloseForRecovery } from '../../../src/voice-error-classifier.js';

/** Gives bodhi's own close handling time to settle before a parked call is redialed. */
export const POST_PARK_REDIAL_DELAY_MS = 1500;

export interface PhoneCallRef {
	callSid: string;
	hangingUp?: boolean;
}

/** A call may be redialled only while it is still registered and not hanging up. */
export function phoneCallIsLive(call: PhoneCallRef, activeCalls: { has(callSid: string): boolean }): boolean {
	return !call.hangingUp && activeCalls.has(call.callSid);
}

/**
 * bodhi's `upstreamRecovery` for one call: a parked upstream is redialed while the call is live,
 * with greeting and injected context held until the caller speaks. The Twilio stream stays attached
 * for the whole call, so there is no idle park.
 */
export function phoneUpstreamRecovery(
	callSession: PhoneCallRef,
	activeCalls: { has(callSid: string): boolean },
): UpstreamRecoveryOptions {
	return {
		isLive: () => phoneCallIsLive(callSession, activeCalls),
		holdSyntheticUntilFreshSpeech: true,
		parkRedialDelayMs: POST_PARK_REDIAL_DELAY_MS,
		idleParkMs: 0,
		// The one Gemini close classifier, the voice agent's.
		classifyClose: fatalCloseForRecovery,
	};
}

/** Logs each park, and runs `onRecovered` when a parked call is back to ACTIVE. */
export function watchPhoneUpstream(deps: {
	eventBus: Pick<IEventBus, 'subscribe'>;
	onRecovered?: () => void;
	log: (msg: string) => void;
}): void {
	let parked = false;
	deps.eventBus.subscribe('session.upstreamLost', (e: EventPayloadMap['session.upstreamLost']) => {
		parked = true;
		deps.log(`[Phone] upstream lost: reason=${e.reason} code=${e.code ?? '-'} detail=${e.detail ?? '-'}`);
	});
	deps.eventBus.subscribe('session.stateChange', (e: EventPayloadMap['session.stateChange']) => {
		if (e.toState !== 'ACTIVE' || !parked) return;
		parked = false;
		deps.onRecovered?.();
	});
}
