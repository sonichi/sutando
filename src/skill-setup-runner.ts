// Shared runner for optional skills' setup() hooks. The isolation guarantee is
// the contract: one bad skill must not crash or stall the host's bootstrap.

import type { ToolDefinition } from 'bodhi-realtime-agent';
import type { ClientFrame, ClientFrameHandler, ClientDisconnectedHandler } from './client-frame-hub.js';

export type SkillSetupCtx = {
	session: unknown;
	injectText: (session: unknown, text: string) => void;
	/** Send one JSON frame to the attached client; false when none took it. */
	sendClientFrame: (frame: ClientFrame) => boolean;
	/** Offered every client JSON frame; return true to claim it. */
	onClientFrame: (handler: ClientFrameHandler) => void;
	onClientDisconnected: (handler: ClientDisconnectedHandler) => void;
	/** Context the model should know, framed as a system line; retried until the session is live. */
	injectContext: (text: string) => void;
};
export type SkillSetup = (ctx: SkillSetupCtx) => void;

/** What a skill's optional `voiceSurface()` export adds to the web voice session only. */
export interface VoiceSurfaceContribution {
	/** Declared on the voice session, never on the phone tool table. */
	tools?: ToolDefinition[];
	/** Rule lines for the voice prompt's RULES block. */
	promptRules?: string[];
	/** Context lines, re-evaluated at every prompt build. */
	contextLines?: () => string[];
}
export type VoiceSurfaceHook = () => VoiceSurfaceContribution;

/** Evaluate each hook under isolation and merge; a throwing or malformed hook contributes nothing. */
export function collectVoiceSurface(
	hooks: readonly VoiceSurfaceHook[],
	log: (msg: string, detail?: unknown) => void = (m, d) => console.error(m, d),
): Required<VoiceSurfaceContribution> {
	const tools: ToolDefinition[] = [];
	const promptRules: string[] = [];
	const contextFns: Array<() => string[]> = [];
	for (const hook of hooks) {
		try {
			const c = hook();
			if (!c || typeof c !== 'object') continue;
			if (Array.isArray(c.tools)) tools.push(...c.tools);
			if (Array.isArray(c.promptRules)) promptRules.push(...c.promptRules.filter(l => typeof l === 'string' && l));
			if (typeof c.contextLines === 'function') contextFns.push(c.contextLines);
		} catch (err) {
			log('[voice-surface] hook threw:', err instanceof Error ? err.message : err);
		}
	}
	const contextLines = (): string[] => {
		const out: string[] = [];
		for (const fn of contextFns) {
			try {
				const lines = fn();
				if (Array.isArray(lines)) out.push(...lines.filter(l => typeof l === 'string' && l));
			} catch (err) {
				log('[voice-surface] contextLines threw:', err instanceof Error ? err.message : err);
			}
		}
		return out;
	};
	return { tools, promptRules, contextLines };
}

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
