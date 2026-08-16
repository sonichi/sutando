/**
 * Stuck-CONNECTING recovery policy for the voice health monitor (#2963).
 *
 * Its own module because `voice-agent.ts` calls `main()` at import time — a test
 * that imported the predicate from there would boot a voice agent.
 */

export const DEFAULT_STUCK_CONNECTING_MS = 120_000;

/** Parse the VOICE_STUCK_CONNECTING_MS override. `0` disables the watchdog —
 *  `Number(x) || default` could not express that, and swallowed typos silently. */
export function parseStuckConnectingMs(
	raw: string | undefined,
	warn: (m: string) => void = console.warn,
): number {
	if (raw === undefined || raw.trim() === '') return DEFAULT_STUCK_CONNECTING_MS;
	const n = Number(raw);
	if (!Number.isFinite(n) || n < 0) {
		warn(`[voice] VOICE_STUCK_CONNECTING_MS=${JSON.stringify(raw)} is not a non-negative number; `
			+ `using ${DEFAULT_STUCK_CONNECTING_MS}ms`);
		return DEFAULT_STUCK_CONNECTING_MS;
	}
	return n;
}

/** How long a session may sit in CONNECTING with a client attached before the
 *  health monitor forces CLOSED. 0 disables; 120s is 4 monitor ticks. */
export const STUCK_CONNECTING_MS = parseStuckConnectingMs(process.env.VOICE_STUCK_CONNECTING_MS);

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
	const threshold = o.thresholdMs ?? STUCK_CONNECTING_MS;
	if (threshold === 0) return false;   // explicitly disabled
	if (o.state !== 'CONNECTING' || !o.clientConnected || o.connectingSince === 0) return false;
	if (o.now - o.connectingSince <= threshold) return false;
	if (o.now - o.lastReconnectAt <= 60_000) return false;
	return o.now > o.fatalBackoffUntil;
}
