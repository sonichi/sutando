/**
 * Stuck-CONNECTING recovery policy for the voice health monitor (#2963).
 *
 * Its own module because `voice-agent.ts` calls `main()` at import time — a test
 * that imported the predicate from there would boot a voice agent.
 */

export const DEFAULT_STUCK_CONNECTING_MS = 120_000;

/** Lower bound for a positive override. bodhi bounds a dial at 30s
 *  (DEFAULT_CONNECT_TIMEOUT_MS; 45s on the reconnect path) — a threshold
 *  below that inverts the backstop into a dial-killer, and the forced CLOSED
 *  cannot bump bodhi's private dial generation, so a late dial RESOLUTION
 *  would land in a session already declared dead. 60s = 2x the dial bound;
 *  `0` remains the explicit disable and is never clamped. */
export const MIN_STUCK_CONNECTING_MS = 60_000;

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
	if (n > 0 && n < MIN_STUCK_CONNECTING_MS) {
		warn(`[voice] VOICE_STUCK_CONNECTING_MS=${JSON.stringify(raw)} is below the safe floor `
			+ `(upstream dial deadline is 30-45s); clamping to ${MIN_STUCK_CONNECTING_MS}ms`);
		return MIN_STUCK_CONNECTING_MS;
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

/** One health-tick step of the hang clock.
 *
 *  The clock keys on STATE alone: a client detaching must NOT reset it — the
 *  session is still hung, and a user reloading the panel (the one
 *  self-recovery action they have) would otherwise restart the countdown and
 *  push recovery out indefinitely. Attachment is checked at FIRE time instead,
 *  via shouldForceClosed. The caller owns the transition and should zero the
 *  clock only after it succeeds, so a failed force keeps the clock armed.
 */
export function nextConnectingTick(o: {
	connectingSince: number; state: string; clientConnected: boolean;
	now: number; lastReconnectAt: number; fatalBackoffUntil: number;
	thresholdMs?: number;
}): { connectingSince: number; forceClose: boolean } {
	if (o.state !== 'CONNECTING') return { connectingSince: 0, forceClose: false };
	if (o.connectingSince === 0) return { connectingSince: o.now, forceClose: false };
	return { connectingSince: o.connectingSince, forceClose: shouldForceClosed(o) };
}
