import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

// Regression guard for the action-grounding + capability-grounding additions
// to the discord-voice owner-tier system prompt (issue #1102).
//
// Observed on Lucy's server (2026-05-25):
//   1. False tool narration: voice said "Opening the Sutando repository" with NO
//      tool_call row in sqlite — model verbally claimed an action it didn't do.
//   2. Feature invention: voice claimed "Za Warudo" activates "co-presentation
//      navigation mode" — a fabricated capability that doesn't exist.
//
// Fix: three explicit grounding guards added to buildAgent()'s owner-tier
// instructions block so the model is explicitly told not to narrate fake actions
// and not to invent undocumented capabilities.

const SRC = readFileSync(
	join(import.meta.dirname ?? '.', '..', 'skills/discord-voice/scripts/discord-voice-server.ts'),
	'utf-8',
);

describe('discord-voice buildAgent — grounding guards (#1102)', () => {
	it('ACTION GROUNDING guard present: no narrating actions without tool invocation', () => {
		assert.match(
			SRC,
			/ACTION GROUNDING.*MUST invoke the corresponding tool.*Do NOT narrate/s,
			'discord-voice-server.ts must include the ACTION GROUNDING instruction in buildAgent.',
		);
	});

	it('CAPABILITY GROUNDING guard present: list only documented tools/commands', () => {
		assert.match(
			SRC,
			/CAPABILITY GROUNDING.*list ONLY those documented in this prompt/s,
			'discord-voice-server.ts must include the CAPABILITY GROUNDING instruction in buildAgent.',
		);
	});

	it('UNCERTAINTY guard present: say I don\'t know rather than guess', () => {
		assert.match(
			SRC,
			/UNCERTAINTY.*uncertain about a fact.*never guess/s,
			'discord-voice-server.ts must include the UNCERTAINTY instruction in buildAgent.',
		);
	});

	it('grounding guards appear in the owner-tier instructions block (before the else branch)', () => {
		const ownerBlock = SRC.match(/function buildAgent[\s\S]+?} else \{/)?.[0] ?? '';
		assert.match(
			ownerBlock,
			/ACTION GROUNDING/,
			'ACTION GROUNDING must be in the owner-tier (if isOwner) block, not the fallback block.',
		);
		assert.match(
			ownerBlock,
			/CAPABILITY GROUNDING/,
			'CAPABILITY GROUNDING must be in the owner-tier block.',
		);
	});

	it('grounding guards reference issue #1102 in the adjacent comment', () => {
		assert.match(
			SRC,
			/#1102/,
			'discord-voice-server.ts must reference issue #1102 in the grounding-guard comment.',
		);
	});
});
