// Shared runner for optional skills' setup() hooks. The isolation guarantee is
// the contract: one bad skill must not crash or stall the host's bootstrap.

export type SkillSetupCtx = { session: unknown; injectText: (session: unknown, text: string) => void };
export type SkillSetup = (ctx: SkillSetupCtx) => void;

function isThenable(v: unknown): v is PromiseLike<unknown> {
	return !!v && (typeof v === 'object' || typeof v === 'function')
		&& typeof (v as { then?: unknown }).then === 'function';
}

/** Run each setup() under isolation. -> count that completed synchronously. */
export function runSkillSetups(
	setups: readonly SkillSetup[],
	ctx: SkillSetupCtx,
	log: (msg: string, detail?: unknown) => void = (m, d) => console.error(m, d),
): number {
	let ok = 0;
	for (const setup of setups) {
		let result: unknown;
		try {
			result = setup(ctx);
		} catch (err) {
			log('[skill-setup] hook threw:', err instanceof Error ? err.message : err);
			continue;
		}
		// `.then` is skill-controlled, so reading it can throw. Inspection stays
		// inside isolation or a throwing getter escapes and kills the whole loop.
		let thenable: boolean;
		try {
			thenable = isThenable(result);
		} catch (err) {
			log('[skill-setup] thenable inspection threw:', err instanceof Error ? err.message : err);
			continue;
		}
		if (thenable) {
			// Not awaited: awaiting a hung skill would stall bootstrap. Assimilate via
			// Promise.resolve so an untrusted `then` rejects instead of throwing here.
			try {
				Promise.resolve(result).then(undefined, (err: unknown) => {
					log('[skill-setup] async hook rejected:', err instanceof Error ? err.message : err);
				});
			} catch (err) {
				log('[skill-setup] thenable assimilation threw:', err instanceof Error ? err.message : err);
			}
			log('[skill-setup] hook returned a thenable; setup() must be synchronous so '
				+ 'registration completes before session start — async work will not be awaited');
			continue;
		}
		ok++;
	}
	return ok;
}
