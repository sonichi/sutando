import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { inlineTools, researchTool } from '../src/inline-tools.js';

// Tier 0.5 gating invariant: the research tool must NOT be part of the default
// inline tool table. Vanilla Tier 0 stays byte-identical; voice-agent.ts adds
// research only when SUTANDO_TIER05 is enabled. If someone accidentally drops
// researchTool into `inlineTools`, this fails and catches the vanilla regression.
describe('cloud-brain research tool (Tier 0.5 gating)', () => {
	it('research tool is well-formed', () => {
		assert.equal(researchTool.name, 'research');
		assert.ok(researchTool.parameters, 'has a parameters schema');
		assert.equal(typeof researchTool.execute, 'function');
	});

	it('is NOT in the default inline tool table (vanilla Tier 0 untouched)', () => {
		assert.ok(
			inlineTools.every((t) => t.name !== 'research'),
			'research must be added only via the SUTANDO_TIER05 gate, never in the default table',
		);
	});
});
