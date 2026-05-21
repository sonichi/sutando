import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { tierFor, toolAllowed, type AccessTiers } from '../skills/discord-voice/scripts/access-tier.js';

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
