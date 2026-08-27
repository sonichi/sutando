/**
 * Event-driven redial scheduler with exponential backoff (F5).
 *
 * The health monitor's redial was blind: a 30s tick + 60s throttle that
 * ignored HOW the last attempt died — up to 60s of dead air after a clean
 * single failure, and a fixed cadence forever after fast re-kills (the
 * 2026-08-17 incident log shows fresh lineages dying in 9s and 11s, where
 * churn may itself be worsening the incident). This module turns bodhi's
 * connection-lifecycle facts (pin 0d506ead) into a dial schedule:
 *
 *  - terminal loss (remote `generation-close`, or `setup-failed`) schedules
 *    a dial after 1s, 2s, 4s, ... capped at 60s, jittered ±20%;
 *  - a `setup-ok` resets the ladder ONLY once the connection has proven
 *    stable (no close for 30s) — otherwise the 9s-death loop would reset
 *    the backoff every cycle and defeat it. The proof is evaluated lazily
 *    at the NEXT close, so no timer is needed;
 *  - bodhi's own `disconnect()` close (1000 "local disconnect") never
 *    schedules — it is the reconnect path working, not a loss;
 *  - the fatal-backoff gate (voiceFatalBackoffUntil) is honoured both when
 *    computing nextDialAt and again at fire time.
 *
 * Pure over injected now()/random() so tests are deterministic. Its own
 * module because voice-agent.ts calls main() at import time (same reason as
 * voice-connect-watchdog.ts). The 30s tick REMAINS as the safety net for
 * states these events miss; it defers to nextDialAt via tickMayDial().
 */

export const REDIAL_BASE_MS = 1_000;
export const REDIAL_CAP_MS = 60_000;
/** How long a connection must survive after setup-ok to reset the ladder. */
export const REDIAL_STABLE_MS = 30_000;
export const REDIAL_JITTER_FRAC = 0.2;

/** Structural subset of bodhi's ConnectionLifecycleEvent — keeps this
 *  module dependency-free while accepting the real union unchanged. */
export interface RedialLifecycleEvent {
	kind: 'attempt' | 'setup-ok' | 'setup-failed' | 'attempt-close' | 'generation-close';
	code?: number;
	reason?: string;
}

export interface RedialState {
	/** Consecutive unstable/failed connection cycles — the ladder index. */
	failures: number;
	/** Epoch ms the next event-driven dial may fire; 0 = none pending. */
	nextDialAt: number;
	/** Epoch ms of the current connection's setup-ok; 0 = none/consumed. */
	setupOkAt: number;
}

export function initialRedialState(): RedialState {
	return { failures: 0, nextDialAt: 0, setupOkAt: 0 };
}

/** bodhi's disconnect() emits exactly this close; it is never a loss. */
export function isLocalDisconnect(ev: RedialLifecycleEvent): boolean {
	return ev.kind === 'generation-close' && ev.code === 1000 && ev.reason === 'local disconnect';
}

/** Ladder delay for the Nth consecutive failure (1-based), jittered ±20%.
 *  The cap applies to the base so jitter stays symmetric at the top. */
export function backoffDelayMs(failures: number, random: () => number = Math.random): number {
	const base = Math.min(REDIAL_CAP_MS, REDIAL_BASE_MS * 2 ** Math.max(0, failures - 1));
	return Math.round(base * (1 + REDIAL_JITTER_FRAC * (2 * random() - 1)));
}

/** One lifecycle event folded into the state. `scheduleDelayMs` is non-null
 *  exactly when the caller should (re)arm its dial timer. */
export function noteLifecycle(
	state: RedialState,
	ev: RedialLifecycleEvent,
	o: { now: number; fatalBackoffUntil?: number; random?: () => number },
): { state: RedialState; scheduleDelayMs: number | null } {
	switch (ev.kind) {
		case 'attempt':
			// A dial is in flight — a stale pending dial must not fire under it.
			// setupOkAt is cleared too: stability credit belongs to the connection
			// that earned it, and the tick's safety-net dial reaches here with the
			// previous setupOkAt still set (no close was observed to consume it).
			return { state: { ...state, nextDialAt: 0, setupOkAt: 0 }, scheduleDelayMs: null };
		case 'setup-ok':
			return { state: { ...state, setupOkAt: o.now, nextDialAt: 0 }, scheduleDelayMs: null };
		case 'attempt-close':
			// The failed-dial verdict is `setup-failed` on the same attempt
			// (bodhi may emit both); acting here would double-schedule. The
			// 30s tick is the net for an attempt-close nothing follows.
			return { state, scheduleDelayMs: null };
		case 'setup-failed':
		case 'generation-close': {
			// The connection is over either way: consume the stability proof.
			const stable = state.setupOkAt > 0 && o.now - state.setupOkAt >= REDIAL_STABLE_MS;
			const priorFailures = stable ? 0 : state.failures;
			if (isLocalDisconnect(ev)) {
				return { state: { ...state, failures: priorFailures, setupOkAt: 0 }, scheduleDelayMs: null };
			}
			const failures = priorFailures + 1;
			const delay = backoffDelayMs(failures, o.random ?? Math.random);
			const nextDialAt = Math.max(o.now + delay, o.fatalBackoffUntil ?? 0);
			return {
				state: { failures, setupOkAt: 0, nextDialAt },
				scheduleDelayMs: nextDialAt - o.now,
			};
		}
	}
}

/** Fire-time gate for the event-driven dial — same preconditions as the
 *  tick's CLOSED guard, minus its 60s throttle (fast redial is the point). */
export function shouldEventDial(o: {
	state: string; clientConnected: boolean; now: number;
	nextDialAt: number; fatalBackoffUntil: number;
}): boolean {
	if (o.nextDialAt === 0 || o.now < o.nextDialAt) return false;
	if (o.state !== 'CLOSED' || !o.clientConnected) return false;
	return o.now > o.fatalBackoffUntil;
}

/** The 30s tick's deference to the scheduler: never dial before a pending
 *  nextDialAt (0 = nothing pending, tick behaves as before). */
export function tickMayDial(o: { now: number; nextDialAt: number }): boolean {
	return o.now >= o.nextDialAt;
}

/** A dial was triggered (either path) — consume the pending schedule. */
export function noteDialed(state: RedialState): RedialState {
	return { ...state, nextDialAt: 0 };
}
