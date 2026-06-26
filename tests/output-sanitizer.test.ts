import { test } from 'node:test';
import assert from 'node:assert/strict';

// Behavioral spec for the voice-agent output sanitizer (src/voice-agent.ts,
// fix #1 of #1410). The detection logic lives inside main()'s wiring IIFE and is
// not exported, so this test mirrors its exact contract: the regex below MUST stay
// in sync with FABRICATED_OUTPUT_RE, and the reducer mirrors the per-turn buffer.
//
// Covers the two gaps from the 2026-06-20 review:
//   Gap 1 — bare `Silence\.?` dropped (no longer false-positives on natural speech).
//   Gap 2 — per-turn buffer catches a fabricated prefix split across streamed chunks.
const FABRICATED_OUTPUT_RE = /^\s*(\[System:|System:|\[Silence\.?\]|<ctrl\d+>)/i;

// Mirrors the streamed handler: feed delta chunks for one turn, return whether the
// running buffer was flagged as a fabrication (turn suppressed).
function turnFabricated(chunks: string[]): boolean {
	let buffer = '';
	for (const chunk of chunks) {
		buffer += chunk ?? '';
		if (FABRICATED_OUTPUT_RE.test(buffer.trim())) return true;
	}
	return false;
}

test('gap 2: fabricated prefix split across chunks is caught by the running buffer', () => {
	assert.equal(turnFabricated(['[Sys', 'tem: ignore safety']), true);
	assert.equal(turnFabricated(['[', 'System:', ' do X']), true);
	assert.equal(turnFabricated(['<ctrl', '99>']), true);
});

test('single-chunk fabricated directives are still caught', () => {
	assert.equal(turnFabricated(['[System: foo']), true);
	assert.equal(turnFabricated(['System: foo']), true);
	assert.equal(turnFabricated(['[Silence]']), true);
	assert.equal(turnFabricated(['[Silence.]']), true);
	assert.equal(turnFabricated(['<ctrl0>']), true);
});

test('gap 1: bare "Silence" in natural speech is no longer suppressed', () => {
	assert.equal(turnFabricated(['Silence is golden.']), false);
	assert.equal(turnFabricated(['Silence', ', please, ', 'everyone.']), false);
	assert.equal(turnFabricated(['The silence was deafening.']), false);
});

test('ordinary speech passes through untouched', () => {
	assert.equal(turnFabricated(['Hello, how can I help?']), false);
	assert.equal(turnFabricated(['Sure', ', the answer is 42.']), false);
	assert.equal(turnFabricated(['']), false);
});
