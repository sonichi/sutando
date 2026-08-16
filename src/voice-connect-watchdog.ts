/**
 * Stuck-CONNECTING recovery policy for the voice health monitor (#2963).
 *
 * Its own module because `voice-agent.ts` calls `main()` at import time — a test
 * that imported the predicate from there would boot a voice agent.
 */

/** How long a session may sit in CONNECTING with a client attached before the
 *  health monitor forces CLOSED. Must exceed a healthy connect by a wide
 *  margin; 120s is 4 monitor ticks. */
export const STUCK_CONNECTING_MS = Number(process.env.VOICE_STUCK_CONNECTING_MS) || 120_000;

/** Whether a CONNECTING session has hung long enough to force CLOSED.
 *
 *  bodhi transitions CLOSED->CONNECTING inline, so a connect that FAILS FAST
 *  flips back and the existing CLOSED guard recovers it. One that HANGS stays
 *  CONNECTING, where that guard can never see it.
 */
export function shouldForceClosed(o: {
	state: string; clientConnected: boolean; connectingSince: number;
	now: number; lastReconnectAt: number; fatalBackoffUntil: number;
	thresholdMs?: number;
}): boolean {
	if (o.state !== 'CONNECTING' || !o.clientConnected || o.connectingSince === 0) return false;
	if (o.now - o.connectingSince <= (o.thresholdMs ?? STUCK_CONNECTING_MS)) return false;
	if (o.now - o.lastReconnectAt <= 60_000) return false;
	return o.now > o.fatalBackoffUntil;
}
