/**
 * Shared session-delivery control flow for live agent runtimes.
 *
 * When a durable result is ready but the live session may not be active yet
 * (e.g. the WebSocket just reconnected but Gemini setup completes ~100ms
 * later), retry the injection on a fixed schedule before giving up to a
 * host-provided fallback. This retry-then-fallback SHAPE is identical across
 * surfaces (web voice, phone, MatrixRTC) — only the `attempt` (how to inject)
 * and `onExhausted` (the fallback) differ — so it lives here, DI'd, alongside
 * the framing helpers.
 *
 * Timing history: the web result-watcher used two attempts at +1.5s and +3s
 * before falling back (a stuck session must not pin the result forever). The
 * attempt is re-evaluated INSIDE each timer, not at schedule time — see the
 * 2026-05-20 02:36:44 incident (voice-test-1779244500.txt "delivered" per the
 * bridge log but never spoken because isActive was false at callback time;
 * Gemini setup completed 106ms later). Delay-then-recheck is what avoids it.
 */

export interface DeliverWithRetryOptions {
	/** Try to deliver now. Return true if it landed (stop), false to keep retrying. */
	attempt: () => boolean;
	/** Called once, after every scheduled attempt has failed. */
	onExhausted: () => void;
	/**
	 * Delays (ms) before each attempt. Default `[1500, 1500]` → attempts at
	 * +1.5s and +3s, then `onExhausted`. A single entry = one attempt then
	 * fallback; an empty array = fallback only (no attempt).
	 */
	delaysMs?: number[];
}

/**
 * Attempt delivery on a fixed schedule; run the fallback once every attempt has
 * failed. Non-blocking — schedules timers and returns. No infinite retry.
 */
export function deliverWithRetry(opts: DeliverWithRetryOptions): void {
	const delays = opts.delaysMs ?? [1500, 1500];
	if (delays.length === 0) {
		opts.onExhausted();
		return;
	}
	const step = (i: number): void => {
		setTimeout(() => {
			if (opts.attempt()) return;
			if (i + 1 < delays.length) {
				step(i + 1);
			} else {
				opts.onExhausted();
			}
		}, delays[i]);
	};
	step(0);
}
