import { describe, it, before, after, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

// MEMORY_DIR in voice-context.ts is captured at import time from SUTANDO_MEMORY_DIR.
// Set env before dynamic import so readMemory() uses our tmpdir.
const TMP = mkdtempSync(join(tmpdir(), 'sutando-voicectx-'));
const PRIOR_MEM = process.env.SUTANDO_MEMORY_DIR;
const PRIOR_WS = process.env.SUTANDO_WORKSPACE;
process.env.SUTANDO_MEMORY_DIR = TMP;
process.env.SUTANDO_WORKSPACE = TMP;

const { buildVoiceAgentContext, buildSutandoSystemPrompt } = await import('../src/voice-context.js');

after(() => {
	if (PRIOR_MEM !== undefined) process.env.SUTANDO_MEMORY_DIR = PRIOR_MEM;
	else delete process.env.SUTANDO_MEMORY_DIR;
	if (PRIOR_WS !== undefined) process.env.SUTANDO_WORKSPACE = PRIOR_WS;
	else delete process.env.SUTANDO_WORKSPACE;
	try { rmSync(TMP, { recursive: true, force: true }); } catch { /* ignore */ }
});

// ── buildVoiceAgentContext ────────────────────────────────────────────────────

describe('buildVoiceAgentContext — no memory files', () => {
	it('returns a string without USER CONTEXT when user_profile.md absent', () => {
		const out = buildVoiceAgentContext();
		assert.equal(typeof out, 'string');
		assert.ok(!out.includes('USER CONTEXT:'), 'should not include USER CONTEXT when profile missing');
	});
});

describe('buildVoiceAgentContext — user profile', () => {
	before(() => {
		writeFileSync(join(TMP, 'user_profile.md'), '---\nname: user_profile\ntype: user\n---\nBassil Khilo — AI Engineer');
	});
	after(() => {
		try { rmSync(join(TMP, 'user_profile.md')); } catch { /* ignore */ }
	});

	it('includes USER CONTEXT when user_profile.md exists', () => {
		const out = buildVoiceAgentContext();
		assert.ok(out.includes('USER CONTEXT:'), 'should include USER CONTEXT:');
		assert.ok(out.includes('Bassil Khilo'), 'should include profile content');
	});

	it('strips YAML frontmatter from profile', () => {
		const out = buildVoiceAgentContext();
		assert.ok(!out.includes('name: user_profile'), 'frontmatter should be stripped');
	});
});

describe('buildVoiceAgentContext — profile truncation', () => {
	const longProfile = 'x'.repeat(600);
	before(() => {
		writeFileSync(join(TMP, 'user_profile.md'), longProfile);
	});
	after(() => {
		try { rmSync(join(TMP, 'user_profile.md')); } catch { /* ignore */ }
	});

	it('truncates user profile at 500 characters', () => {
		const out = buildVoiceAgentContext();
		assert.ok(out.includes('USER CONTEXT:'));
		// The 600-char string sliced to 500 should be present, remainder should not
		assert.ok(out.includes('x'.repeat(500)));
		assert.ok(!out.includes('x'.repeat(501)));
	});
});

describe('buildVoiceAgentContext — phone calls', () => {
	before(() => {
		const callsDir = join(TMP, 'results', 'calls');
		mkdirSync(callsDir, { recursive: true });
		const entries = [
			JSON.stringify({ caller: 'Alice', start_time: '2026-05-20T10:00:00Z', summary: 'Discussed Q2 roadmap' }),
			JSON.stringify({ to: '+1234567890', timestamp: '2026-05-21T12:00:00Z', topic: 'Follow-up on PR review' }),
			'not-valid-json',
		];
		writeFileSync(join(callsDir, 'calls.jsonl'), entries.join('\n'));
	});
	after(() => {
		try { rmSync(join(TMP, 'results'), { recursive: true, force: true }); } catch { /* ignore */ }
	});

	it('includes RECENT PHONE CALLS section', () => {
		const out = buildVoiceAgentContext();
		assert.ok(out.includes('RECENT PHONE CALLS:'), 'should include phone calls section');
	});

	it('shows caller names and summaries', () => {
		const out = buildVoiceAgentContext();
		assert.ok(out.includes('Alice'), 'should show caller name');
		assert.ok(out.includes('Q2 roadmap'), 'should show call summary');
	});

	it('falls back to to/topic fields when caller/summary absent', () => {
		const out = buildVoiceAgentContext();
		assert.ok(out.includes('+1234567890') || out.includes('Follow-up on PR review'),
			'should handle to/topic fields');
	});

	it('skips malformed JSON lines without throwing', () => {
		assert.doesNotThrow(() => buildVoiceAgentContext());
	});
});

describe('buildVoiceAgentContext — no calls file', () => {
	it('returns string without RECENT PHONE CALLS when file absent', () => {
		// No calls.jsonl in TMP (cleaned up by prior after())
		const out = buildVoiceAgentContext();
		assert.ok(!out.includes('RECENT PHONE CALLS:'), 'should omit section when calls file missing');
	});
});

// ── buildSutandoSystemPrompt ──────────────────────────────────────────────────

describe('buildSutandoSystemPrompt — base output', () => {
	it('always includes the base rules section', () => {
		const out = buildSutandoSystemPrompt();
		assert.ok(out.includes('You are Sutando'), 'should include Sutando identity');
		assert.ok(out.includes('Rules:'), 'should include Rules: section');
	});

	it('always includes memory dir reference', () => {
		const out = buildSutandoSystemPrompt();
		assert.ok(out.includes('memory'), 'should mention memory system');
	});
});

describe('buildSutandoSystemPrompt — memory sections', () => {
	before(() => {
		writeFileSync(join(TMP, 'user_profile.md'), 'Bassil Khilo — growth engineer');
		writeFileSync(join(TMP, 'feedback_response_style.md'), 'Be concise and direct');
		writeFileSync(join(TMP, 'feedback_minimal_cost_max_value.md'), 'Do less — minimal change');
	});
	after(() => {
		for (const f of ['user_profile.md', 'feedback_response_style.md', 'feedback_minimal_cost_max_value.md']) {
			try { rmSync(join(TMP, f)); } catch { /* ignore */ }
		}
	});

	it('includes ## User context section when user_profile exists', () => {
		const out = buildSutandoSystemPrompt();
		assert.ok(out.includes('## User context'), 'should have ## User context header');
		assert.ok(out.includes('Bassil Khilo'), 'should include profile content');
	});

	it('includes ## Communication style when response_style exists', () => {
		const out = buildSutandoSystemPrompt();
		assert.ok(out.includes('## Communication style'), 'should have ## Communication style header');
		assert.ok(out.includes('Be concise and direct'), 'should include style content');
	});

	it('includes ## Working style when feedback_minimal_cost exists', () => {
		const out = buildSutandoSystemPrompt();
		assert.ok(out.includes('## Working style'), 'should have ## Working style header');
		assert.ok(out.includes('Do less'), 'should include working style content');
	});
});

describe('buildSutandoSystemPrompt — strips frontmatter from memory files', () => {
	before(() => {
		writeFileSync(join(TMP, 'user_profile.md'),
			'---\nname: user_profile\ntype: user\n---\nReal content here');
	});
	after(() => {
		try { rmSync(join(TMP, 'user_profile.md')); } catch { /* ignore */ }
	});

	it('does not include YAML frontmatter in output', () => {
		const out = buildSutandoSystemPrompt();
		assert.ok(!out.includes('name: user_profile'), 'should strip frontmatter keys');
		assert.ok(out.includes('Real content here'), 'should include body content');
	});
});
