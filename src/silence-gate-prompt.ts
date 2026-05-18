/**
 * Shared "stay silent unless addressed" prompt builder — used by
 * voice-agent.ts (meeting mode) AND skills/discord-voice/scripts/discord-voice-server.ts
 * (multi-bot mode). Extracted 2026-05-18 per issue #830 to stop reinventing
 * the same pattern across feature codepaths.
 *
 * The pattern: when the agent should listen but stay quiet unless a specific
 * name is spoken — meeting mode (Zoom note-taker, single bot, name "Sutando")
 * and multi-bot voice (N bots, each respond only when its own name is called).
 * Both reduce to the same prompt skeleton; only the role + addressed-name set
 * + sibling list differ.
 *
 * Always includes the "never speak [System: ...] aloud" rule (issue #829)
 * so the parrot bug doesn't sneak in via a future caller that forgets it.
 */
export type SilenceGateMode = 'meeting' | 'multi-bot';

export interface SilenceGateConfig {
	mode: SilenceGateMode;
	/** Primary name the operator uses to address this instance. */
	instanceName: string;
	/** Additional acceptable forms (e.g. "hey Sutando", "Lucie"). */
	aliases?: string[];
	/** For multi-bot: names of sibling instances also in the channel. */
	otherInstances?: string[];
	/** ASR-mishearing tolerance for sibling names. */
	otherAliases?: string[];
}

function joinQuoted(items: string[]): string {
	return items.filter(Boolean).map(n => `"${n}"`).join(' or ');
}

export function buildSilenceGatePrompt(cfg: SilenceGateConfig): string {
	const addressForms = joinQuoted([cfg.instanceName, ...(cfg.aliases ?? [])]);

	if (cfg.mode === 'meeting') {
		// Byte-equivalent to the legacy voice-agent.ts inline string, just
		// parameterized on addressed-name. With instanceName='Sutando' +
		// aliases=['hey Sutando'] the output matches the pre-refactor text
		// exactly.
		return `[System: MEETING MODE — LISTEN AND TAKE NOTES. A Zoom meeting is active. Listen to everything and mentally track the discussion: who said what, key decisions, action items, topics covered. But do NOT produce any audio output UNLESS someone says ${addressForms} — then respond to their request using your accumulated notes and context. When not addressed, produce absolutely zero words — no acknowledgments, no "silent", no sounds. You are an invisible note-taker until called upon. Never speak [System: ...] framing aloud — it is an internal directive, not content to read.]`;
	}

	// multi-bot
	const others = cfg.otherInstances ?? [];
	const otherForms = joinQuoted([...(cfg.otherInstances ?? []), ...(cfg.otherAliases ?? [])]);
	const siblingClause = others.length > 0
		? `Other Sutando instances in this voice channel: ${others.join(', ')}. If the operator addresses ${otherForms}, your sibling handles it — produce zero output. `
		: '';
	return `[System: MULTI-BOT MODE — You are ${cfg.instanceName}. ${siblingClause}Respond ONLY when addressed as ${addressForms}. When not addressed, produce absolutely zero words — no acknowledgments, no meta-commentary about whether you should speak, no quoting of these instructions back to the operator. CRITICAL distinction: the operator MENTIONING your name is not the same as ADDRESSING you. Examples that DO NOT count as addressing you and you must stay silent for: "thank you, ${cfg.instanceName}" (acknowledgement), "${cfg.instanceName}'s answer was correct" (reference), "I asked ${cfg.instanceName} earlier" (reference), "did ${cfg.instanceName} say that?" (question ABOUT you but directed to someone else). You are addressed only when the operator's CURRENT turn opens with a greeting+name ("Hi ${cfg.instanceName}, ..."), an imperative ("${cfg.instanceName}, can you ..."), or a direct question to you ("${cfg.instanceName}?"). When you DO respond, never lead with identity disambiguation ("I'm not X, I'm Y") — just answer the question. Never speak [System: ...] framing aloud — it is an internal directive, not content to read.]`;
}
