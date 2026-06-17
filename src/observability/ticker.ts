/**
 * Generic interval-based usage ticker — emit usage records while a session is
 * live instead of accumulating everything into one end-of-session burst.
 *
 * `startTicker` is surface-agnostic: callers supply an `onTick` callback that
 * receives the elapsed ms and bucket-start ms for the current window and returns
 * whatever record(s) they want to write. Voice, phone, screen-capture, API-
 * gateway — any streaming surface that bills by time can use this.
 *
 * Usage:
 *   const ticker = startTicker(
 *     (durationMs, bucketStartMs) => record({ meter: 'my.seconds', quantity: durationMs / 1000, ... }),
 *     60_000,
 *   );
 *   // ...session runs, ticker fires every 60s automatically...
 *   ticker.stop(); // emits the final partial bucket, cancels the interval
 */

/** Default interval between usage ticks (30 seconds). */
export const USAGE_TICK_MS = 30_000;

export interface TickerHandle<T> {
	/**
	 * Emit the final partial bucket and cancel the interval.
	 * Idempotent — returns `undefined` on every call after the first.
	 */
	stop: () => T | undefined;
}

/**
 * Start a periodic usage ticker. `onTick` is called on every interval AND on
 * `stop()`, receiving `(durationMs, bucketStartMs)` for the current window.
 * The return value of `onTick` is passed back to the caller from `stop()`.
 *
 * `_nowFn` defaults to `Date.now` and is injectable for deterministic tests.
 */
export function startTicker<T>(
	onTick: (durationMs: number, bucketStartMs: number) => T,
	intervalMs = USAGE_TICK_MS,
	_nowFn: () => number = Date.now,
): TickerHandle<T> {
	let lastMs = _nowFn();
	let stopped = false;
	const timer = setInterval(() => {
		const now = _nowFn();
		try { onTick(now - lastMs, lastMs); } catch { /* non-throwing by contract; belt-and-suspenders */ }
		lastMs = now;
	}, intervalMs);
	// Defense-in-depth: a leaked ticker (caller forgot stop()) must not keep the
	// Node process alive on its own. unref() lets the event loop exit anyway.
	timer.unref?.();
	return {
		stop: () => {
			if (stopped) return undefined;
			stopped = true;
			clearInterval(timer);
			const now = _nowFn();
			try { return onTick(now - lastMs, lastMs); } catch { return undefined; }
		},
	};
}
