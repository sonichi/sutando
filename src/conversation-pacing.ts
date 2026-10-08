/**
 * When a background result may enter the conversation: not while either side is speaking,
 * only after a few quiet seconds, and longer after an interrupted answer (the user's question
 * may still be open). A cap keeps a noisy room from holding a result forever.
 */

export interface ConversationPacerOptions {
	/** Silence needed after the last speech or turn, in ms. */
	quietMs?: number;
	/** Silence needed after an interrupted answer, counted from the interruption. */
	afterInterruptMs?: number;
	/** Longest a result waits for quiet before it is delivered anyway. */
	maxWaitMs?: number;
	pollMs?: number;
	now?: () => number;
}

export function createConversationPacer(opts: ConversationPacerOptions = {}) {
	const quietMs = opts.quietMs ?? 5_000;
	const afterInterruptMs = opts.afterInterruptMs ?? 15_000;
	const maxWaitMs = opts.maxWaitMs ?? 60_000;
	const pollMs = opts.pollMs ?? 500;
	const now = opts.now ?? Date.now;
	let modelSpeaking = false;
	let userSpeaking = false;
	let lastActivityAt = -Infinity;
	let interruptedAt = -Infinity;
	const touch = () => { lastActivityAt = now(); };

	const pacer = {
		onTurnStart() { modelSpeaking = true; touch(); },
		onTurnEnd() { modelSpeaking = false; interruptedAt = -Infinity; touch(); },
		onTurnInterrupted() { modelSpeaking = false; touch(); interruptedAt = lastActivityAt; },
		onUserSpeechStarted() { userSpeaking = true; touch(); },
		onUserSpeechEnded() { userSpeaking = false; touch(); },
		isQuiet(): boolean {
			if (modelSpeaking || userSpeaking) return false;
			const t = now();
			return t - lastActivityAt >= quietMs && t - interruptedAt >= afterInterruptMs;
		},
		/** Resolves once the conversation is quiet, or after maxWaitMs. */
		waitForQuiet(): Promise<void> {
			const startedAt = now();
			return new Promise((resolve) => {
				const check = () => {
					if (pacer.isQuiet() || now() - startedAt >= maxWaitMs) return resolve();
					setTimeout(check, pollMs);
				};
				check();
			});
		},
	};
	return pacer;
}
