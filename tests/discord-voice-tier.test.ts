import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
	tierFor,
	toolAllowed,
	toolNeed,
	SKILL_TOOL_TIER,
	mostRestrictiveTier,
	effectiveTier,
	type AccessTiers,
} from '../skills/discord-voice/scripts/access-tier.js';

// Synthetic ids — the logic doesn't care about the id shape.
const access: AccessTiers = {
	owner: 'owner-0001',
	team: new Set(['owner-0001', 'peer-bot-A', 'peer-bot-B', 'collaborator-1']),
};

describe('tierFor', () => {
	it('the owner id → owner', () => {
		assert.equal(tierFor('owner-0001', access), 'owner');
	});
	it('an allowFrom id that is not the owner → team', () => {
		assert.equal(tierFor('peer-bot-A', access), 'team');
		assert.equal(tierFor('collaborator-1', access), 'team');
	});
	it('an unknown id → other', () => {
		assert.equal(tierFor('stranger-9999', access), 'other');
	});
	it('undefined speaker → other', () => {
		assert.equal(tierFor(undefined, access), 'other');
	});
	it('with no owner configured, the owner path never matches', () => {
		const noOwner: AccessTiers = { owner: '', team: new Set(['peer-bot-A']) };
		assert.equal(tierFor('peer-bot-A', noOwner), 'team');
		assert.equal(tierFor('anyone-else', noOwner), 'other');
	});
});

describe('toolAllowed', () => {
	it('owner-only tool — only the owner', () => {
		assert.equal(toolAllowed('owner', 'owner'), true);
		assert.equal(toolAllowed('owner', 'team'), false);
		assert.equal(toolAllowed('owner', 'other'), false);
	});
	it('team tool — owner and team, not other', () => {
		assert.equal(toolAllowed('team', 'owner'), true);
		assert.equal(toolAllowed('team', 'team'), true);
		assert.equal(toolAllowed('team', 'other'), false);
	});
	it('open tool (need=null) — everyone', () => {
		assert.equal(toolAllowed(null, 'owner'), true);
		assert.equal(toolAllowed(null, 'team'), true);
		assert.equal(toolAllowed(null, 'other'), true);
	});
});

describe('SKILL_TOOL_TIER — per-tool tiering of skill-local tools', () => {
	it('dismiss is team-tier (policy: a teammate may end the session)', () => {
		assert.equal(SKILL_TOOL_TIER.dismiss, 'team');
	});
	it('work + screen-share tools are owner-only', () => {
		assert.equal(SKILL_TOOL_TIER.work, 'owner');
		assert.equal(SKILL_TOOL_TIER.share_screen, 'owner');
		assert.equal(SKILL_TOOL_TIER.summon, 'owner');
		assert.equal(SKILL_TOOL_TIER.stop_share_screen, 'owner');
	});
});

describe('mostRestrictiveTier', () => {
	it('empty → other (fail closed)', () => {
		assert.equal(mostRestrictiveTier([]), 'other');
	});
	it('single tier → itself', () => {
		assert.equal(mostRestrictiveTier(['owner']), 'owner');
		assert.equal(mostRestrictiveTier(['team']), 'team');
		assert.equal(mostRestrictiveTier(['other']), 'other');
	});
	it('mixed → the least-privileged present', () => {
		assert.equal(mostRestrictiveTier(['owner', 'team']), 'team');
		assert.equal(mostRestrictiveTier(['owner', 'other']), 'other');
		assert.equal(mostRestrictiveTier(['team', 'other']), 'other');
		assert.equal(mostRestrictiveTier(['owner', 'team', 'other']), 'other');
	});
});

describe('effectiveTier — per-turn speaker attribution', () => {
	it('a turn with only the owner → owner', () => {
		assert.equal(effectiveTier(['owner-0001'], access, false), 'owner');
	});
	it('a turn with only a team speaker → team', () => {
		assert.equal(effectiveTier(['peer-bot-A'], access, false), 'team');
	});
	it('a turn with only a stranger → other', () => {
		assert.equal(effectiveTier(['stranger-9999'], access, false), 'other');
	});
	it('TOCTOU guard: non-owner + owner in the same turn → most-restrictive, never owner', () => {
		// A non-owner asks for an owner-tier tool; the owner makes a sound
		// before execute(). Both ids are attributed to the turn → team, so
		// the owner-tier tool is denied (no privilege escalation).
		assert.equal(effectiveTier(['stranger-9999', 'owner-0001'], access, false), 'other');
		assert.equal(effectiveTier(['peer-bot-A', 'owner-0001'], access, false), 'team');
	});
	it('empty speaker set → other (fail closed)', () => {
		assert.equal(effectiveTier([], access, false), 'other');
	});
	it('legacy DISCORD_VOICE_OWNER (treatAsOwner) overrides everything to owner', () => {
		assert.equal(effectiveTier(['stranger-9999'], access, true), 'owner');
		assert.equal(effectiveTier([], access, true), 'owner');
		const noOwner: AccessTiers = { owner: '', team: new Set() };
		assert.equal(effectiveTier(['anyone'], noOwner, true), 'owner');
	});
});

describe('toolNeed — full tool classification', () => {
	const ownerOnly = new Set(['some_owner_only_tool']);
	const team = new Set(['some_configurable_tool']);
	it('dismiss → team, work → owner', () => {
		assert.equal(toolNeed('dismiss', ownerOnly, team), 'team');
		assert.equal(toolNeed('work', ownerOnly, team), 'owner');
	});
	it('registry ownerOnly tool → owner; configurable tool → team', () => {
		assert.equal(toolNeed('some_owner_only_tool', ownerOnly, team), 'owner');
		assert.equal(toolNeed('some_configurable_tool', ownerOnly, team), 'team');
	});
	it('an unclassified (inline read-only) tool → null (open to all)', () => {
		assert.equal(toolNeed('get_current_time', ownerOnly, team), null);
	});
});
