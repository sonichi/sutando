// The phone server's upstream-recovery wiring, importable without booting the server.
import type { EventPayloadMap, IEventBus } from 'bodhi-realtime-agent';
import { createPostParkRedialer, type RecoveryLog, type RecoverySurface } from '../../../src/voice-upstream-recovery.js';

/** Gives bodhi's own close handling time to settle before the host redials a parked session. */
export const POST_PARK_REDIAL_DELAY_MS = 1500;

export type PhoneRecoverySession = RecoverySurface & { eventBus: Pick<IEventBus, 'subscribe'> };

export interface PhoneCallRef {
	callSid: string;
	hangingUp?: boolean;
}

/** A call may be redialled only while it is still registered and not hanging up. */
export function phoneCallIsLive(call: PhoneCallRef, activeCalls: { has(callSid: string): boolean }): boolean {
	return !call.hangingUp && activeCalls.has(call.callSid);
}

/** Subscribes to `session.upstreamLost` and redials the parked call, holding greeting and context until the caller speaks. */
export function wirePhoneUpstreamRecovery(
	deps: {
		session: PhoneRecoverySession;
		callSession: PhoneCallRef;
		activeCalls: { has(callSid: string): boolean };
		onActivated?: () => void;
		schedule?: (fn: () => void, ms: number) => unknown;
	} & RecoveryLog,
): () => 'recover' | 'none' | 'skipped' {
	const { session, callSession, activeCalls } = deps;
	const redialAfterPark = createPostParkRedialer({
		getSession: () => session,
		isLive: () => phoneCallIsLive(callSession, activeCalls),
		origin: `Phone ${callSession.callSid}`,
		hold: true,
		onActivated: deps.onActivated,
		log: deps.log,
		error: deps.error,
	});
	const schedule = deps.schedule ?? ((fn, ms) => setTimeout(fn, ms));
	session.eventBus.subscribe('session.upstreamLost', (e: EventPayloadMap['session.upstreamLost']) => {
		deps.log(`[Phone] upstream lost: reason=${e.reason} code=${e.code ?? '-'} detail=${e.detail ?? '-'}`);
		schedule(redialAfterPark, POST_PARK_REDIAL_DELAY_MS);
	});
	return redialAfterPark;
}
