/**
 * Per-speaker access tiering for discord-voice — pure, testable logic split
 * out of discord-voice-server.ts so it can be unit-tested without a live
 * voice session.
 *
 * Mirrors the discord-bridge access model exactly (discord-bridge.py), read
 * from the same ~/.claude/channels/discord/access.json:
 *   owner — top-level `allowFrom` (canonical owner tier — discord-bridge.py
 *           treats top-level allowFrom as owner; access.json's `owner` field
 *           is not the tier source)
 *   team  — the union of `groups[*].allowFrom` (per-channel trusted circle)
 *   other — anyone else who speaks in the channel
 * Owner takes precedence: an id in both resolves to owner.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

export type Tier = 'owner' | 'team' | 'other';

export interface AccessTiers {
	owner: Set<string>;
	team: Set<string>;
}

/**
 * Read access.json under the given home dir. Fail-soft → empty tiers.
 * owner = top-level `allowFrom`; team = union of `groups[*].allowFrom`.
 * Matches discord-bridge.py so the same access.json can't be read two ways.
 */
export function loadAccessTiers(homeDir: string): AccessTiers {
	try {
		const p = join(homeDir, '.claude/channels/discord/access.json');
		const a = JSON.parse(readFileSync(p, 'utf-8'));
		const owner = new Set<string>((a.allowFrom ?? []).map(String));
		const team = new Set<string>();
		for (const cfg of Object.values(a.groups ?? {})) {
			if (cfg && typeof cfg === 'object') {
				for (const id of ((cfg as { allowFrom?: unknown[] }).allowFrom ?? [])) {
					team.add(String(id));
				}
			}
		}
		return { owner, team };
	} catch {
		return { owner: new Set<string>(), team: new Set<string>() };
	}
}

/** Tier of a speaking Discord user id. Owner is checked first (precedence). */
export function tierFor(userId: string | undefined, access: AccessTiers): Tier {
	if (!userId) return 'other';
	if (access.owner.has(userId)) return 'owner';
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

const TIER_RANK: Record<Tier, number> = { other: 0, team: 1, owner: 2 };

/** Least-privileged tier among the given tiers; 'other' if empty (fail closed). */
export function mostRestrictiveTier(tiers: Tier[]): Tier {
	let lo: Tier = 'other';
	let loRank = Infinity;
	for (const t of tiers) {
		if (TIER_RANK[t] < loRank) { loRank = TIER_RANK[t]; lo = t; }
	}
	return lo;
}

/**
 * Effective tier of a turn, given every speaker who contributed audio to it.
 * Fails closed: the least-privileged speaker governs the whole turn, so a
 * non-owner cannot inherit owner tier just because the owner also made a
 * sound before the tool's execute() ran, and an empty set (no attributed
 * speaker) resolves to 'other'. The legacy DISCORD_VOICE_OWNER escape hatch
 * (treatAsOwner) overrides everything to owner.
 */
export function effectiveTier(
	speakerIds: Iterable<string>,
	access: AccessTiers,
	treatAsOwner: boolean,
): Tier {
	if (treatAsOwner) return 'owner';
	return mostRestrictiveTier([...speakerIds].map(id => tierFor(id, access)));
}

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
