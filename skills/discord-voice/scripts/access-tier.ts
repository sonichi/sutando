/**
 * Per-speaker access tiering for discord-voice — pure, testable logic split
 * out of discord-voice-server.ts so it can be unit-tested without a live
 * voice session.
 *
 * Mirrors the discord-bridge access model, read from the same
 * ~/.claude/channels/discord/access.json:
 *   owner — the `owner` field (this instance's single operator)
 *   team  — the rest of `allowFrom` (trusted circle: peers, collaborators)
 *   other — anyone else who speaks in the channel
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

export type Tier = 'owner' | 'team' | 'other';

export interface AccessTiers {
	owner: string;
	team: Set<string>;
}

/** Read access.json under the given home dir. Fail-soft → empty tiers. */
export function loadAccessTiers(homeDir: string): AccessTiers {
	try {
		const p = join(homeDir, '.claude/channels/discord/access.json');
		const a = JSON.parse(readFileSync(p, 'utf-8'));
		return { owner: String(a.owner ?? ''), team: new Set<string>(a.allowFrom ?? []) };
	} catch {
		return { owner: '', team: new Set<string>() };
	}
}

/** Tier of a speaking Discord user id. */
export function tierFor(userId: string | undefined, access: AccessTiers): Tier {
	if (!userId) return 'other';
	if (access.owner && userId === access.owner) return 'owner';
	if (access.team.has(userId)) return 'team';
	return 'other';
}

/**
 * May a speaker of `tier` use a tool requiring `need`?
 *   need=null  — open tool, anyone
 *   need='owner' — owner only
 *   need='team'  — owner or team
 */
export function toolAllowed(need: Tier | null, tier: Tier): boolean {
	if (need === null) return true;
	if (need === 'owner') return tier === 'owner';
	return tier !== 'other';
}

/**
 * Minimum tier for the skill-local discord-voice tools. Per the access-tier
 * policy: `dismiss` is team-tier (a teammate may end the voice session);
 * `work` + the screen-share tools are owner-only. Tools from the core
 * inline-tools registry are classified separately (see `toolNeed`).
 */
export const SKILL_TOOL_TIER: Record<string, Tier> = {
	work: 'owner',
	share_screen: 'owner',
	summon: 'owner',
	stop_share_screen: 'owner',
	dismiss: 'team',
};

/**
 * Minimum tier a tool requires, or null if open to every tier.
 *   ownerOnly / team — tool-name sets from the core inline-tools registry
 *                      (`ownerOnlyTools` / `configurableTools`).
 * Skill-local discord-voice tools are classified by SKILL_TOOL_TIER.
 */
export function toolNeed(name: string, ownerOnly: Set<string>, team: Set<string>): Tier | null {
	if (name in SKILL_TOOL_TIER) return SKILL_TOOL_TIER[name];
	if (ownerOnly.has(name)) return 'owner';
	if (team.has(name)) return 'team';
	return null;
}
