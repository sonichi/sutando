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
